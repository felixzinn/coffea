import os
import re
import textwrap
import signal

from tempfile import NamedTemporaryFile, TemporaryDirectory
from os.path import basename, join, getsize

from collections import namedtuple

import math
import numpy
import scipy
import random

from tqdm.auto import tqdm

import cloudpickle

from .executor import (
    WorkItem,
    _compression_wrapper,
    _decompress,
)

from .accumulator import (
    accumulate,
)


# The Work Queue object is global b/c we want to
# retain state between runs of the executor, such
# as connections to workers, cached data, etc.
_wq_queue = None

# If set to True, workflow stops processing and outputs only the results that
# have been already processed.
early_terminate = False


# This function, that accumulates results from files does not require wq.
# We declare it before checking for wq so that we do not need to install wq at
# the remote site.
def accumulate_result_files(files_to_accumulate, accumulator=None):
    from coffea.processor import accumulate

    # work on local copy of list
    files_to_accumulate = list(files_to_accumulate)
    while files_to_accumulate:
        f = files_to_accumulate.pop()

        with open(f, "rb") as rf:
            result = _decompress(rf)

        if not accumulator:
            accumulator = result
            continue

        accumulator = accumulate([result_f], accumulator)
        del result
    return accumulator


try:
    from work_queue import WorkQueue, Task
    import work_queue as wq
except ImportError:
    wq = None
    print("work_queue module not available")

    class Task:
        def __init__(self, *args, **kwargs):
            raise ImportError("work_queue not available")

    class WorkQueue:
        def __init__(self, *args, **kwargs):
            raise ImportError("work_queue not available")


TaskReport = namedtuple("TaskReport", ["events_count", "wall_time", "memory"])


class CoffeaWQ(WorkQueue):
    def __init__(
        self,
        executor,
    ):
        self.executor = executor
        self.console = VerbosePrint(
            executor.status, executor.verbose or executor.print_stdout
        )
        self.stats_coffea = Stats()

        self.tasks_to_accumulate = []
        self.task_reports = []

        if not self.executor.port:
            self.executor.port = 0 if self.executor.master_name else 9123

        self._staging_dir_obj = TemporaryDirectory("wq-tmp-", dir=executor.filepath)

        super().__init__(
            port=self.executor.port,
            name=self.executor.master_name,
            debug_log=self.executor.debug_log,
            stats_log=self.executor.stats_log,
            transactions_log=self.executor.transactions_log,
            status_display_interval=self.executor.status_display_interval,
            ssl=self.executor.ssl,
        )

        self._declare_resources()

        # Make use of the stored password file, if enabled.
        if self.executor.password_file:
            self.specify_password_file(self.executor.password_file)

        self.function_wrapper = self._write_fn_wrapper()

        if self.executor.tasks_accum_log:
            with open(self.executor.tasks_accum_log, "w") as f:
                f.write(
                    "id,category,status,dataset,file,range_start,range_stop,accum_parent,time_start,time_end,cpu_time,memory,fin,fout\n"
                )

        self.console.printf(f"Listening for work queue workers on port {self.port}.")
        # perform a wait to print any warnings before progress bars
        self.wait(0)

    def __del__(self):
        try:
            self._staging_dir_obj.cleanup()
        finally:
            super().__del__()

    def submit(self, task):
        taskid = super().submit(task)
        self.console(
            "submitted {category} task id {id} item {item}, with {size} {unit}",
            category=task.category,
            id=taskid,
            item=task.itemid,
            size=len(task),
            units=self.executor.unit,
        )
        return taskid

    def wait(self, timeout=None):
        task = super().wait(timeout)
        if task:
            # Evaluate and display details of the completed task
            if task.successful():
                task.fout_size = getsize(task.outfile_output) / 1e6
                if task.fin_size > 0:
                    # record only if task used any intermediate inputs
                    self.stats_coffea.max("size_max_input", task.fin_size)
                self.stats_coffea.max("size_max_output", task.fout_size)
            task.report(self.executor.print_stdout, self.executor.resource_monitor)
            # Remove input files as we go to avoid unbounded disk we do not
            # remove outputs, as they are used by further accumulate tasks
            task.cleanup_inputs()
            return task
        return None

    def application_info(self):
        return {
            "application_info": {
                "values": dict(self.stats_coffea),
                "units": {
                    "size_max_output": "MB",
                    "size_max_input": "MB",
                },
            }
        }

    def staging_dir(self):
        return self._staging_dir_obj.name

    def function_to_file(self, function, name=None):
        with NamedTemporaryFile(
            prefix=name, suffix=".p", dir=self.staging_dir, delete=False
        ) as f:
            cloudpickle.dump(function, f)
            return f.name

    def _add_task_report(self, task):
        r = TaskReport(
            len(task), task.cmd_execution_time / 1e6, task.resources_measured.memory
        )
        self.task_reports.append(r)

    def _preprocessing(self, items, function, accumulator):
        preprocessing_bar = tqdm(
            desc="Preprocessing",
            total=len(items),
            disable=not self.executor.status,
            unit=self.executor.unit,
            bar_format=self.executor.bar_format,
        )

        function = _compression_wrapper(self.executor.compression, function)
        infile_procc_fn = self.function_to_file(function, "preproc")
        for item in items:
            task = PreProcCoffeaWQTask(self, infile_procc_fn, item)
            self.submit(task)

        while not self.empty():
            task = self.wait(5)
            if task:
                if task.successful():
                    accumulator = accumulate([task.output], accumulator)
                    preprocessing_bar.update(1)
                    task.cleanup_outputs()
                    task.task_accum_log(self.executor.tasks_accum_log, "", "done")
                else:
                    task.resubmit(self)

        preprocessing_bar.close()
        return accumulator

    def _submit_processing_tasks(self, infile_procc_fn, items):
        while True:
            if early_terminate:
                return
            if not self.hungry():
                return
            if self.stats.get("events_queued") >= self.stats.get("events_total"):
                return

            if (
                self.executor.dynamic_chunksize
                and self.stats_coffea.get("events_queued") > 0
            ):
                # can't send if generator not initialized first with a next
                chunksize = self.current_chunksize
                if self.executor.dynamic_chunksize:
                    chunksize = _sample_chunksize(self.current_chunksize)
                item = items.send(chunksize)
            else:
                item = next(items)

            self._submit_processing_task(infile_procc_fn, item)

    def _submit_processing_task(self, infile_procc_fn, item):
        t = ProcCoffeaWQTask(self, infile_procc_fn, item)
        self.submit(t)
        self.stats_coffea.inc("events_queued", len(t))
        self.progress_bars["submitted"].update(len(t))

    def _final_accumulation(self, accumulator):
        if len(self.tasks_to_accumulate) < 1:
            raise RuntimeError("No results available.")

        self.console("Merging with final accumulator...")
        accumulator = accumulate_result_files(
            [t.outfile_output for t in self.tasks_to_accumulate], accumulator
        )

        for t in self.tasks_to_accumulate:
            t.cleanup_outputs()
            t.task_accum_log(self.executor.tasks_accum_log, "accumulated", 0)

        self.progress_bars["accumulated"].update(1)
        self.progress_bars["accumulated"].refresh()

        return accumulator

    def _processing(self, items, function, accumulator):
        function = _compression_wrapper(self.executor.compression, function)
        accumulate_fn = _compression_wrapper(
            self.executor.compression, accumulate_result_files
        )

        infile_procc_fn = self.function_to_file(function, "proc")
        infile_accum_fn = self.function_to_file(accumulate_fn, "accum")

        # if not dynamic_chunksize, ensure that the items looks like a generator
        if isinstance(items, list):
            items = iter(items)

        executor = self.executor
        stats = self.stats_coffea

        # Keep track of total tasks in each state.
        stats.set("events_queued", 0)
        stats.set("events_processed", 0)
        stats.set("events_accumulated", 0)
        stats.set("events_total", executor.events_total)

        stats.set("original_chunksize", executor.chunksize)
        stats.set("current_chunksize", executor.chunksize)

        self.progress_bars = _make_progress_bars(executor)
        signal.signal(signal.SIGINT, _handle_early_terminate)

        self._process_events(infile_procc_fn, infile_accum_fn, items)

        # merge results with original accumulator given by the executor
        accumulator = self._final_accumulation(accumulator)

        self.console("done")

        return accumulator

    def _process_events(self, infile_procc_fn, infile_accum_fn, items):
        while self.coffea_stats.get("events_accumulated") < self.coffea_stats.get(
            "events_total"
        ):
            if early_terminate and self.empty():
                # all pending accumulation tasks after getting the signal have
                # finished
                break

            self._submit_processing_tasks(infile_procc_fn)

            # When done submitting, look for completed tasks.
            task = self.wait(5)

            # refresh progress bars
            for bar in self.progress_bars.values():
                bar.update(0)

            if not task:
                continue

            if not task.successful():
                task.resubmit(self)
                continue

            self.tasks_to_accumulate.append(task)

            if re.match("processing", task.category):
                self._add_task_report(task)
                self.coffea_stats.inc("events_processed", len(task))
                self.progress_bars["processing"].update(len(task))
                self._update_chunksize()
            else:
                self.coffea_stats.inc("events_accumulated", len(task))
                self.progress_bars["accumulating"].update(1)

            self._submit_accum_tasks(infile_accum_fn)

        if self.coffea_stats.get("events_processed") < self.coffea_stats.get(
            "events_total"
        ):
            self.console.printf("\nWARNING: Not all items were processed.\n")

        self.console("final chunksize {}", self.current_chunksize)

    def _submit_accum_tasks(self, infile_accum_fn):
        chunks_per_accum = self.executor.chunks_per_accum

        stats = self.coffea_stats

        force = (
            stats.get("events_processed") >= stats.get("events_total")
        ) or early_terminate
        force = early_terminate
        force |= stats.get("events_processed") >= stats.get("events_total")

        if len(self.tasks_to_accumulate) < 2 * chunks_per_accum - 1 and not force:
            return

        self.tasks_to_accumulate.sort(key=lambda t: t.fout_size)

        for next_to_accum in _group_lst(self.tasks_to_accumulate, chunks_per_accum):
            # return immediately if not enough for a single accumulation
            # this can only happen in the last group
            if len(next_to_accum) < 2 or (
                len(next_to_accum) < chunks_per_accum and not force
            ):
                self.tasks_to_accumulate = next_to_accum
                break

            accum_task = AccumCoffeaWQTask(self, infile_accum_fn, next_to_accum)
            self.submit(accum_task)

            # log the input tasks to this accumulation task
            for t in next_to_accum:
                t.task_accum_log(self.executor.tasks_accum_log, "done", t.id)

            acc_sub = self.stats_category("accumulating").tasks_submitted
            self.progress_bars["accumulating"].total = math.ceil(
                1 + (stats.get("events_total") * acc_sub / stats.get("events_done"))
            )

    def _update_chunksize(self):
        ex = self.executor
        if ex.dynamic_chunksize:
            chunksize = _compute_chunksize(
                ex.chunksize, ex.dynamic_chunksize, self.task_reports
            )
            self.current_chunksize = chunksize
            self.stats_coffea.set("current_chunksize", chunksize)
            self.console("current chunksize {}", chunksize)
        return self.current_chunksize

    def _declare_resources(self):
        executor = self.executor

        # If explicit resources are given, collect them into default_resources
        default_resources = {}
        if executor.cores:
            default_resources["cores"] = executor.cores
        if executor.memory:
            default_resources["memory"] = executor.memory
        if executor.disk:
            default_resources["disk"] = executor.disk
        if executor.gpus:
            default_resources["gpus"] = executor.gpus

        # Enable monitoring and auto resource consumption, if desired:
        self.tune("category-steady-n-tasks", 3)

        # Evenly divide resources in workers per category
        self.tune("force-proportional-resources", 1)

        # if resource_monitor is given, and not 'off', then monitoring is activated.
        # anything other than 'measure' is assumed to be 'watchdog' mode, where in
        # addition to measuring resources, tasks are killed if they go over their
        # resources.
        monitor_enabled = True
        watchdog_enabled = True
        if not executor.resource_monitor or executor.resource_monitor == "off":
            monitor_enabled = False
        elif executor.resource_monitor == "measure":
            watchdog_enabled = False

        # activate monitoring if it has not been explicitely activated and we are
        # using an automatic resource allocation.
        if executor.resources_mode != "fixed":
            monitor_enabled = True

        if monitor_enabled:
            self.enable_monitoring(watchdog=watchdog_enabled)

        # set the auto resource modes
        mode = wq.WORK_QUEUE_ALLOCATION_MODE_MAX
        if executor.resources_mode == "fixed":
            mode = wq.WORK_QUEUE_ALLOCATION_MODE_FIXED
        for category in "default preprocessing processing accumulating".split():
            self.specify_category_max_resources(category, default_resources)
            self.specify_category_mode(category, mode)
        # use auto mode max-throughput only for processing tasks
        if executor.resources_mode == "max-throughput":
            self.specify_category_mode(
                "processing", wq.WORK_QUEUE_ALLOCATION_MODE_MAX_THROUGHPUT
            )

        # enable fast termination of workers
        fast_terminate = executor.fast_terminate_workers
        for category in "default preprocessing processing accumulating".split():
            if fast_terminate and fast_terminate > 1:
                self.activate_fast_abort_category(category, fast_terminate)

    def _write_fn_wrapper(self):
        """Writes a wrapper script to run serialized python functions and arguments.
        The wrapper takes as arguments the name of three files: function, argument, and output.
        The files function and argument have the serialized function and argument, respectively.
        The file output is created (or overwritten), with the serialized result of the function call.
        The wrapper created is created/deleted according to the lifetime of the WorkQueueExecutor."""

        proxy_basename = ""
        if self.executor.x509_proxy:
            proxy_basename = basename(self.executor.x509_proxy)

        contents = textwrap.dedent(
            """\
                        #!/usr/bin/env python3
                        import os
                        import sys
                        import cloudpickle
                        import coffea

                        if "{proxy}":
                            os.environ['X509_USER_PROXY'] = "{proxy}"

                        (fn, args, out) = sys.argv[1], sys.argv[2], sys.argv[3]

                        with open(fn, 'rb') as f:
                            exec_function = cloudpickle.load(f)
                        with open(args, 'rb') as f:
                            exec_args = cloudpickle.load(f)

                        pickled_out = exec_function(*exec_args)
                        with open(out, 'wb') as f:
                            f.write(pickled_out)

                        # Force an OS exit here to avoid a bug in xrootd finalization
                        os._exit(0)
                        """
        )
        with NamedTemporaryFile(
            prefix="fn_wrapper", dir=self.staging_dir, delete=False
        ) as f:
            f.write(contents.format(proxy=proxy_basename).encode())
            return f.name


class CoffeaWQTask(Task):
    tasks_counter = 0

    def __init__(self, queue, infile_procc_fn, item_args, itemid):
        CoffeaWQTask.tasks_counter += 1

        self.itemid = itemid

        self.py_result = ResultUnavailable()
        self._stdout = None

        self.infile_procc_fn = infile_procc_fn

        self.infile_args = join(queue.staging_dir, "args_{}.p".format(self.itemid))
        self.outfile_output = join(queue.staging_dir, "out_{}.p".format(self.itemid))
        self.outfile_stdout = join(queue.staging_dir, "stdout_{}.p".format(self.itemid))

        with open(self.infile_args, "wb") as wf:
            cloudpickle.dump(item_args, wf)

        executor = queue.executor
        self.retries_to_go = executor.retries

        super().__init__(self.remote_command(env_file=executor.environment_file))

        self.specify_input_file(queue.function_wrapper, "fn_wrapper", cache=False)
        self.specify_input_file(infile_procc_fn, "function.p", cache=False)
        self.specify_input_file(self.infile_args, "args.p", cache=False)
        self.specify_output_file(self.outfile_output, "output.p", cache=False)
        self.specify_output_file(self.outfile_stdout, "stdout.log", cache=False)

        for f in executor.extra_input_files:
            self.specify_input_file(f, cache=True)

        if executor.x509_proxy:
            self.specify_input_file(executor.x509_proxy, cache=True)

        if executor.wrapper and executor.environment_file:
            self.specify_input_file(executor.wrapper, "py_wrapper", cache=True)
            self.specify_input_file(executor.environment_file, "env_file", cache=True)

    def __len__(self):
        return self.size

    def __str__(self):
        return str(self.itemid)

    def remote_command(self, env_file=None):
        fn_command = "python fn_wrapper function.p args.p output.p >stdout.log 2>&1"
        command = fn_command

        if env_file:
            wrap = (
                './py_wrapper -d -e env_file -u "$WORK_QUEUE_SANDBOX"/{}-env-{} -- {}'
            )
            command = wrap.format(basename(env_file), os.getpid(), fn_command)

        return command

    @property
    def std_output(self):
        if not self._stdout:
            try:
                with open(self.outfile_stdout, "r") as rf:
                    self._stdout = rf.read()
            except Exception:
                self._stdout = None
        return self._stdout

    def _has_result(self):
        return not (
            self.py_result is None or isinstance(self.py_result, ResultUnavailable)
        )

    # use output to return python result, rathern than stdout as regular wq
    @property
    def output(self):
        if not self._has_result():
            try:
                with open(self.outfile_output, "rb") as rf:
                    result = _decompress(rf)
                    self.py_result = result
            except Exception as e:
                self.py_result = ResultUnavailable(e)
        return self.py_result

    def cleanup_inputs(self):
        os.remove(self.infile_args)

    def cleanup_outputs(self):
        os.remove(self.outfile_output)

    def resubmit(self, queue):
        if self.retries_to_go < 1 or not queue.executor.split_on_exhaustion:
            raise RuntimeError(
                "item {} failed permanently. No more retries left.".format(self.itemid)
            )

        resubmissions = []
        if self.result == wq.WORK_QUEUE_RESULT_RESOURCE_EXHAUSTION:
            queue.console("splitting {} to reduce resource consumption.", self.itemid)
            resubmissions = self.split(queue)
        else:
            t = self.clone(queue)
            t.retries_to_go = self.retries_to_go - 1
            resubmissions = [t]

        for t in resubmissions:
            queue.console(
                "resubmitting {} partly as {} with {} events. {} attempt(s) left.",
                self.itemid,
                t.itemid,
                len(t),
                t.retries_to_go,
            )
            queue.submit(t)

    def clone(self, queue):
        raise NotImplementedError

    def split(self, queue):
        raise RuntimeError("task cannot be split any further.")

    def debug_info(self):
        self.output  # load results, if needed

        has_output = "" if self._has_result() else "out"
        msg = "{} with{} result.".format(self.itemid, has_output)
        return msg

    def successful(self):
        return (self.result == 0) and (self.return_status == 0)

    def report(self, queue):
        if (not queue.console.verbose_mode) and self.successful():
            return self.successful()

        queue.console.printf(
            "{} task id {} item {} with {} events completed on {}. return code {}",
            self.category,
            self.id,
            self.itemid,
            len(self),
            self.hostname,
            self.return_status,
        )

        queue.console.printf(
            "    allocated cores: {}, memory: {} MB, disk: {} MB, gpus: {}",
            self.resources_allocated.cores,
            self.resources_allocated.memory,
            self.resources_allocated.disk,
            self.resources_allocated.gpus,
        )

        if queue.executor.resource_monitor and queue.executor.resource_monitor != "off":
            queue.console.printf(
                "    measured cores: {}, memory: {} MB, disk {} MB, gpus: {}, runtime {}",
                self.resources_measured.cores,
                self.resources_measured.memory,
                self.resources_measured.disk,
                self.resources_measured.gpus,
                (self.cmd_execution_time) / 1e6,
            )

        if queue.executor.print_stdout or (not self.successful()):
            if self.std_output:
                queue.console.print("    output:")
                queue.console.print(self.std_output)

        if not self.successful():
            # Note that WQ already retries internal failures.
            # If we get to this point, it's a badly formed task
            info = self.debug_info()
            queue.console.printf(
                "task id {} item {} failed: {}\n    {}",
                self.id,
                self.itemid,
                self.result_str,
                info,
            )

        return self.successful()

    def task_accum_log(self, log_filename, accum_parent, status):
        # Should call write_task_accum_log with the appropiate arguments
        return NotImplementedError

    def write_task_accum_log(
        self, log_filename, accum_parent, dataset, filename, start, stop, status
    ):
        if not log_filename:
            return

        with open(log_filename, "a") as f:
            f.write(
                "{id},{cat},{status},{set},{file},{start},{stop},{accum},{time_start},{time_end},{cpu},{mem},{fin},{fout}\n".format(
                    id=self.id,
                    cat=self.category,
                    status=status,
                    set=dataset,
                    file=filename,
                    start=start,
                    stop=stop,
                    accum=accum_parent,
                    time_start=self.resources_measured.start,
                    time_end=self.resources_measured.end,
                    cpu=self.resources_measured.cpu_time,
                    mem=self.resources_measured.memory,
                    fin=self.fin_size,
                    fout=self.fout_size,
                )
            )


class PreProcCoffeaWQTask(CoffeaWQTask):
    infile_procc_fn = None

    def __init__(self, queue, infile_procc_fn, item, itemid=None):
        if not itemid:
            itemid = "pre_{}".format(CoffeaWQTask.tasks_counter)

        self.item = item

        self.size = 1
        super().__init__(queue, infile_procc_fn, [item], itemid)

        self.specify_category("preprocessing")

        if re.search("://", item.filename) or os.path.isabs(item.filename):
            # This looks like an URL or an absolute path (assuming shared
            # filesystem). Not transfering file.
            pass
        else:
            self.specify_input_file(
                item.filename, remote_name=item.filename, cache=True
            )

        self.fin_size = 0

    def clone(self, queue):
        return PreProcCoffeaWQTask(
            queue,
            self.infile_procc_fn,
            self.item,
            self.itemid,
        )

    def debug_info(self):
        i = self.item
        msg = super().debug_info()
        return "{} {}".format((i.dataset, i.filename, i.treename), msg)

    def task_accum_log(self, log_filename, accum_parent, status):
        meta = list(self.output)[0].metadata
        i = self.item
        self.write_task_accum_log(
            log_filename, "", i.dataset, i.filename, 0, meta["numentries"], "done"
        )


class ProcCoffeaWQTask(CoffeaWQTask):
    def __init__(self, queue, infile_procc_fn, item, itemid=None):
        self.size = len(item)

        if not itemid:
            itemid = "p_{}".format(CoffeaWQTask.tasks_counter)

        self.item = item

        super().__init__(queue, infile_procc_fn, [item], itemid)

        self.specify_category("processing")

        if re.search("://", item.filename) or os.path.isabs(item.filename):
            # This looks like an URL or an absolute path (assuming shared
            # filesystem). Not transfering file.
            pass
        else:
            self.specify_input_file(
                item.filename, remote_name=item.filename, cache=True
            )

        self.fin_size = 0

    def clone(self, queue):
        return ProcCoffeaWQTask(
            queue,
            self.infile_procc_fn,
            self.item,
            self.itemid,
        )

    def split(self, queue):
        total = len(self.item)

        if total < 2:
            raise RuntimeError("processing task cannot be split any further.")

        # if the chunksize was updated to be less than total, then use that.
        # Otherwise, just partition the task in two.
        target_chunksize = queue.current_chunksize
        if total <= target_chunksize:
            target_chunksize = math.ceil(total / 2)

        n = max(math.ceil(total / target_chunksize), 1)
        actual_chunksize = int(math.ceil(total / n))

        queue.stats_coffea.inc("chunks_split")
        queue.stats_coffea.min("min_chunksize_after_split", actual_chunksize)

        splits = []
        start = self.item.entrystart
        while start < self.item.entrystop:
            stop = min(self.item.entrystop, start + actual_chunksize)

            w = WorkItem(
                self.item.dataset,
                self.item.filename,
                self.item.treename,
                start,
                stop,
                self.item.fileuuid,
                self.item.usermeta,
            )

            t = self.__class__(queue, self.infile_procc_fn, w)

            start = stop
            splits.append(t)

        return splits

    def debug_info(self):
        i = self.item
        msg = super().debug_info()
        return "{} {}".format(
            (i.dataset, i.filename, i.treename, i.entrystart, i.entrystop), msg
        )

    def task_accum_log(self, log_filename, accum_parent, status):
        i = self.item
        self.write_task_accum_log(
            log_filename,
            accum_parent,
            i.dataset,
            i.filename,
            i.entrystart,
            i.entrystop,
            status,
        )


class AccumCoffeaWQTask(CoffeaWQTask):
    def __init__(
        self,
        queue,
        infile_procc_fn,
        tasks_to_accumulate,
        itemid=None,
    ):
        if not itemid:
            itemid = "accum_{}".format(CoffeaWQTask.tasks_counter)

        self.tasks_to_accumulate = tasks_to_accumulate
        self.size = sum(len(t) for t in self.tasks_to_accumulate)

        args = [[basename(t.outfile_output) for t in self.tasks_to_accumulate]]

        super().__init__(queue, infile_procc_fn, args, itemid)

        self.specify_category("accumulating")

        for t in self.tasks_to_accumulate:
            self.specify_input_file(t.outfile_output, cache=False)

        self.fin_size = sum(t.fout_size for t in tasks_to_accumulate)

    def cleanup_inputs(self):
        super().cleanup_inputs()
        # cleanup files associated with results already accumulated
        for t in self.tasks_to_accumulate:
            t.cleanup_outputs()

    def clone(self, queue):
        return AccumCoffeaWQTask(
            queue,
            self.infile_procc_fn,
            self.tasks_to_accumulate,
            self.itemid,
        )

    def debug_info(self):
        tasks = self.tasks_to_accumulate

        msg = super().debug_info()

        results = [
            CoffeaWQTask.debug_info(t)
            for t in tasks
            if isinstance(t, AccumCoffeaWQTask)
        ]
        results += [
            t.debug_info() for t in tasks if not isinstance(t, AccumCoffeaWQTask)
        ]

        return "{} accumulating: [{}] ".format(msg, "\n".join(results))

    def task_accum_log(self, log_filename, status, accum_parent=None):
        self.write_task_accum_log(
            log_filename, accum_parent, "", "", 0, len(self), status
        )


def run(executor, items, function, accumulator):
    """Execute using Work Queue
    For more information, see :ref:`intro-coffea-wq`
    """
    if not wq:
        print("You must have Work Queue installed to use WorkQueueExecutor!")
        # raise an import error for work queue
        import work_queue

    if executor.environment_file and not executor.environment_file.wrapper:
        raise ValueError(
            "Location of python_package_run could not be determined automatically.\nUse 'wrapper' argument to the work_queue_executor."
        )

    if executor.compression is None:
        executor.compression = 1

    if executor.chunks_per_accum < 2:
        executor.chunks_per_accum = 2

    executor.x509_proxy = _get_x509_proxy(executor.x509_proxy)

    if _wq_queue is None:
        _wq_queue = CoffeaWQ(executor)

    _wq_queue.declare_resources(executor)

    try:
        if executor.custom_init:
            executor.custom_init(_wq_queue)

        if executor.desc == "Preprocessing":
            result = _wq_queue._preprocessing(items, function, accumulator)
            # we do not shutdown queue after preprocessing, as we want to
            # keep the connected workers for processing/accumulation
        else:
            result = _wq_queue._processing(items, function, accumulator)
            _wq_queue = None
    except Exception as e:
        _wq_queue = None
        raise e
    finally:
        for bar in _wq_queue.progress_bars.values():
            bar.close()

    return result


def _handle_early_terminate(signum, frame):
    global early_terminate

    if early_terminate:
        raise KeyboardInterrupt
    else:
        _wq_queue.console.printf(
            "********************************************************************************"
        )
        _wq_queue.console.printf("Canceling processing tasks for final accumulation.")
        _wq_queue.console.printf("C-c again to immediately terminate.")
        _wq_queue.console.printf(
            "********************************************************************************"
        )
        early_terminate = True
        _wq_queue.cancel_by_category("processing")


def _group_lst(lst, n):
    """Split the lst into sublists of len n."""
    return (lst[i : i + n] for i in range(0, len(lst), n))


def _get_x509_proxy(x509_proxy=None):
    if x509_proxy:
        return x509_proxy

    x509_proxy = os.environ.get("X509_USER_PROXY", None)
    if x509_proxy:
        return x509_proxy

    x509_proxy = join(
        os.environ.get("TMPDIR", "/tmp"), "x509up_u{}".format(os.getuid())
    )
    if os.path.exists(x509_proxy):
        return x509_proxy

    return None


def _make_progress_bars(executor):
    items_total = executor.events_total
    status = executor.status
    unit = executor.unit
    bar_format = executor.bar_format
    chunksize = executor.chunksize
    chunks_per_accum = executor.chunks_per_accum

    submit_bar = tqdm(
        total=items_total,
        disable=not status,
        unit=unit,
        desc="Submitted",
        bar_format=bar_format,
        miniters=1,
    )

    processed_bar = tqdm(
        total=items_total,
        disable=not status,
        unit=unit,
        desc="Processed",
        bar_format=bar_format,
    )

    accumulated_bar = tqdm(
        total=1 + int(items_total / (chunksize * chunks_per_accum)),
        disable=not status,
        unit="task",
        desc="Accumulated",
        bar_format=bar_format,
    )

    return {
        "submitted": submit_bar,
        "processing": processed_bar,
        "accumulating": accumulated_bar,
    }


def _check_dynamic_chunksize_targets(targets):
    if targets:
        for k in targets:
            if k not in ["wall_time", "memory"]:
                raise KeyError("dynamic chunksize resource {} is unknown.".format(k))


class ResultUnavailable(Exception):
    pass


class Stats(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def inc(self, stat, delta=1):
        try:
            self[stat] += delta
        except KeyError:
            self[stat] = delta

    def set(self, stat, value):
        self[stat] = value

    def get(self, stat, default=None):
        return self.setdefault(stat, 0)

    def min(self, stat, value):
        try:
            self[stat] = min(self[stat], value)
        except KeyError:
            self[stat] = value

    def max(self, stat, value):
        try:
            self[stat] = max(self[stat], value)
        except KeyError:
            self[stat] = value


class VerbosePrint:
    def __init__(self, status_mode=True, verbose_mode=True):
        self.status_mode = status_mode
        self.verbose_mode = verbose_mode

    def __call__(self, format_str, *args, **kwargs):
        if self.verbose_mode:
            self.printf(format_str, *args, **kwargs)

    def print(self, msg):
        if self.status_mode:
            tqdm.write(msg)
        else:
            print(msg)

    def printf(self, format_str, *args, **kwargs):
        msg = format_str.format(*args, **kwargs)
        self.print(msg)


# Functions related to dynamic chunksize, independent of Work Queue


def _floor_to_pow2(value):
    if value < 1:
        return 1
    return pow(2, math.floor(math.log2(value)))


def _sample_chunksize(chunksize):
    # sample between value found and half of it, to better explore the
    # space.  we take advantage of the fact that the function that
    # generates chunks tries to have equally sized work units per file.
    # Most files have a different number of events, which is unlikely
    # to be a multiple of the chunsize computed. Just in case all files
    # have the same number of events, we return chunksize/2 10% of the
    # time.
    return int(random.choices([chunksize, max(chunksize / 2, 1)], weights=[90, 10])[0])


def _compute_chunksize(base_chunksize, resource_targets, task_reports):
    chunksize_time = None
    chunksize_memory = None

    if resource_targets is not None and len(task_reports) > 1:
        target_time = resource_targets.get("wall_time", None)
        if target_time:
            chunksize_time = _compute_chunksize_target(
                target_time, [(time, evs) for (evs, time, mem) in task_reports]
            )

        target_memory = resource_targets["memory"]
        if target_memory:
            chunksize_memory = _compute_chunksize_target(
                target_memory, [(mem, evs) for (evs, time, mem) in task_reports]
            )

    candidate_sizes = [c for c in [chunksize_time, chunksize_memory] if c]
    if candidate_sizes:
        chunksize = min(candidate_sizes)
    else:
        chunksize = base_chunksize

    try:
        chunksize = int(_floor_to_pow2(chunksize))
    except ValueError:
        chunksize = base_chunksize

    return chunksize


def _compute_chunksize_target(target, pairs):
    # if no info to compute dynamic chunksize (e.g. they info is -1), return nothing
    if len(pairs) < 1 or pairs[0][0] < 0:
        return None

    avgs = [e / max(1, target) for (target, e) in pairs]
    quantiles = numpy.quantile(avgs, [0.25, 0.5, 0.75], interpolation="nearest")

    # remove outliers below the 25%
    pairs_filtered = []
    for (i, avg) in enumerate(avgs):
        if avg >= quantiles[0]:
            pairs_filtered.append(pairs[i])

    try:
        # separate into time, numevents arrays
        slope, intercept, r_value, p_value, std_err = scipy.stats.linregress(
            [rep[0] for rep in pairs_filtered],
            [rep[1] for rep in pairs_filtered],
        )
    except Exception:
        slope = None

    if (
        slope is None
        or numpy.isnan(slope)
        or numpy.isnan(intercept)
        or slope < 0
        or intercept > 0
    ):
        # we assume that chunksize and target have a positive
        # correlation, with a non-negative overhead (-intercept/slope). If
        # this is not true because noisy data, use the avg chunksize/time.
        # slope and intercept may be nan when data falls in a vertical line
        # (specially at the start)
        slope = quantiles[1]
        intercept = 0

    org = (slope * target) + intercept

    return org
