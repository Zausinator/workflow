import sys
import os
import warnings

from ase.atoms import Atoms

from wfl.configset import ConfigSet, OutputSpec
from .utils import grouper
from .remoteinfo import RemoteInfo
from .pool import do_in_pool

from expyre import ExPyRe


def do_remotely(remote_info, hash_ignore=[], num_inputs_per_python_subprocess=1, iterable=None, outputspec=None, op=None, iterable_arg=0,
                skip_failed=True, initializer=None, initargs=None, args=[], kwargs={}, quiet=False):
    """run tasks as series of remote jobs

    Parameters
    ----------
    remote_info: RemoteInfo or dict
        object with all information on remote job, including system, resources, job num_inputs_per_python_subprocess, etc, or dict of kwargs for its constructor
    quiet: bool, default False
        do not output (to stderr) progress info

    See autoparallelize.autoparallelize() for other args
    """
    if ExPyRe is None:
        raise RuntimeError('Cannot run as remote jobs since expyre module could not be imported')

    if not isinstance(remote_info, RemoteInfo):
        remote_info = RemoteInfo(**remote_info)

    if remote_info.num_inputs_per_queued_job < 0:
        remote_info.num_inputs_per_queued_job = -remote_info.num_inputs_per_queued_job * num_inputs_per_python_subprocess

    if isinstance(iterable, ConfigSet):
        items_inputs_generator = grouper(remote_info.num_inputs_per_queued_job, ((item, iterable.get_current_input_file()) for item in iterable))
    else:
        items_inputs_generator = grouper(remote_info.num_inputs_per_queued_job, ((item, None) for item in iterable))

    # create all jobs (count on expyre detection of identical jobs to avoid rerunning things unnecessarily)
    xprs = []
    # place to keep track of input files, one per input item, so that output can go to corresponding file
    input_files = []
    # list of all items, wastes space so used only if remote_info.skip_failures is True
    all_items = []
    for chunk_i, items_gen in enumerate(items_inputs_generator):
        items = []
        for (item, cur_input_file) in items_gen:
            if isinstance(item, Atoms) and 'EXPYRE_REMOTE_JOB_FAILED' in item.info:
                del item.info['EXPYRE_REMOTE_JOB_FAILED']

            items.append(item)
            input_files.append(cur_input_file)

        if remote_info.skip_failures:
            all_items.append(items)

        job_name = remote_info.job_name + f'_chunk_{chunk_i}'
        if not quiet:
            sys.stderr.write(f'Creating job {job_name}\n')

        if isinstance(iterable, ConfigSet):
            job_iterable = ConfigSet(input_configs=items)
        else:
            job_iterable = items
        co = OutputSpec()
        # remote job will have to set num_python_subprocesses appropriately for its node
        # ignore configset out for hashing of inputs, since that doesn't affect function
        # calls that have to happen (also it's not repeatable for some reason)
        xprs.append(ExPyRe(name=job_name, pre_run_commands=remote_info.pre_cmds, post_run_commands=remote_info.post_cmds,
                            hash_ignore=hash_ignore + ['outputspec'],
                            env_vars=remote_info.env_vars, input_files=remote_info.input_files,
                            output_files=remote_info.output_files, function=do_in_pool,
                            kwargs={'num_python_subprocesses': None, 'num_inputs_per_python_subprocess': num_inputs_per_python_subprocess, 'iterable': job_iterable,
                                    'outputspec': co, 'op': op, 'iterable_arg': iterable_arg,
                                    'skip_failed': skip_failed, 'initializer': initializer,
                                    'initargs': initargs, 'args': args, 'kwargs': kwargs}))

    # start jobs (shouldn't do anything if they've already been started)
    for xpr in xprs:
        if not quiet:
            sys.stderr.write(f'Starting job for {xpr.id}\n')
        xpr.start(resources=remote_info.resources, system_name=remote_info.sys_name, header_extra=remote_info.header_extra,
                  exact_fit=remote_info.exact_fit, partial_node=remote_info.partial_node)

    # gather results and write them to original outputspec
    outputspec.pre_write()
    at_i = 0
    for chunk_i, xpr in enumerate(xprs):
        if not quiet:
            sys.stderr.write(f'Gathering results for {xpr.id}\n')

        try:
            ats_out, stdout, stderr = xpr.get_results(timeout=remote_info.timeout, check_interval=remote_info.check_interval)
        except Exception as exc:
            warnings.warn(f'Failed in remote job {xpr.id} on {xpr.system_name}')
            if not remote_info.skip_failures:
                raise
            if len(all_items) > 0 and isinstance(all_items[chunk_i][0], Atoms):
                # get ready to write input configs to output
                ats_out = ConfigSet(input_configs=all_items[chunk_i])
                for at in ats_out:
                    at.info['EXPYRE_REMOTE_JOB_FAILED'] = True
            else:
                # either no inputs saved or inputs aren't configurations, so skip output
                ats_out = None
            stdout = ''
            stderr = ''

        if ats_out is None:
            # Skip the right number of input files. If we're here,
            # remote_info.skip_failures must be True, so all_items should be filled
            at_i += len(all_items[chunk_i])
        else:
            for at in ats_out.group_iter():
                outputspec.write(at, from_input_file=input_files[at_i])
                at_i += 1
            sys.stdout.write(stdout)
            sys.stderr.write(stderr)

    outputspec.end_write()

    if 'WFL_EXPYRE_NO_MARK_PROCESSED' not in os.environ:
        # mark as processed only after outputspec has been finished
        for xpr in xprs:
            xpr.mark_processed()

    return outputspec.to_ConfigSet()
