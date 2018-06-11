import collections
import functools
import sys
import time
import types
import weakref

import tensorflow as tf


def main_loop(sess=None, sess_creator=None, ops=(), hooks=None):
    if sess is None:
        sess = MonitoredSession(sess_creator, hooks=hooks)
    else:
        sess = wrap_session_with_hooks(sess, hooks=hooks)

    with sess:
        # logging.debug('Starting loop')
        while not sess.should_stop():
            # logging.debug('sess.run')
            sess.run(ops)


async def run(*args, **kwargs):
    _, run_values = await run_detailed(*args, **kwargs)
    return run_values.results


@types.coroutine
def run_detailed(*args, **kwargs):
    return (yield tf.train.SessionRunArgs(*args, **kwargs))


async def run_iter(*args, **kwargs):
    try:
        while True:
            yield await run(*args, **kwargs)
    except SessionEndException:
        return


async def run_detailed_iter(*args, **kwargs):
    try:
        while True:
            yield await run_detailed(*args, **kwargs)
    except SessionEndException:
        return


async def sleep(secs):
    await sleep_until(time.time() + secs)


async def sleep_until(t):
    while time.time() < t:
        await run([])


def periodic_wrap(corofun, every_n_secs=None, every_n_steps=None):
    if every_n_steps is None and every_n_secs is None:
        return corofun

    @types.coroutine
    def wrapped(*args, **kwargs):
        coro = corofun(*args, **kwargs)
        last = None
        while True:
            last = yield coro.send(last)
            yield from skip(every_n_secs, every_n_steps)

    return wrapped


async def skip(secs=None, steps=None):
    if steps is None:
        print('await sleep')
        await sleep(secs)
    elif secs is None:
        for i in range(steps):
            await run([])
    else:
        step = 0
        end = time.time() + secs
        while step < steps and time.time() < end:
            await run([])
            step += 1


# CamelCase for consistency
def MonitoredSession(*args, hooks=None, **kwargs):
    return tf.train.MonitoredSession(*args, hooks=[AsyncMultiHook(hooks)], **kwargs)


def coroutine_hook(f):
    @functools.wraps(f)
    def wrapper(*args, every_n_steps=None, every_n_secs=None, **kwargs):
        coro_hook = CoroutineHook(coroutine_fun=f, args=args, kwargs=kwargs)
        if every_n_steps is not None or every_n_secs is not None:
            return _PeriodicHookWrapper(
                coro_hook, every_n_steps=every_n_steps, every_n_secs=every_n_secs)
        else:
            return coro_hook

    return wrapper


def generator_hook(f):
    return functools.wraps(f)(coroutine_hook(types.coroutine(f)))


def wrap_session_with_hooks(sess, hooks):
    return _HookWrappedExistingSession(sess, hooks=[AsyncMultiHook(hooks)])


class RequestLoopStop(Exception):
    def __init__(self, value=None):
        self.value = value


class SessionEndException(Exception):
    def __init__(self, sess):
        self._session = sess

    @property
    def session(self):
        return self._session


class CoroutineHook(tf.train.SessionRunHook):
    def __init__(self, coroutine_fun=None, coroutine=None, args=None, kwargs=None):
        self._coroutine_fun = coroutine_fun
        self._args = args or []
        self._kwargs = kwargs or {}
        self._coroutine = coroutine
        self._finished = None
        self._run_args = None
        self._requested_stop = None
        self._result = None

    def begin(self):
        if self._coroutine_fun:
            self._coroutine = self._coroutine_fun(*self._args, **self._kwargs)

        self._finished = False
        self._requested_stop = False
        self._result = None

        # the first call has to be here, so the hook can add ops to the graph:
        self._run_args = self._get_next_run_args(None)

    def before_run(self, run_context):
        return self._run_args

    def after_run(self, run_context, run_values):
        self._run_args = self._get_next_run_args((run_context, run_values))
        if self._requested_stop:
            run_context.request_stop()

    def end(self, session):
        self._run_args = self._get_next_run_args(SessionEndException(session))

    def _get_next_run_args(self, value):
        if self._finished:
            return None
        try:
            if isinstance(value, Exception):
                return self._coroutine.throw(value)
            else:
                return self._coroutine.send(value)
        except StopIteration as ex:
            # The coroutine returned
            self._result = ex.value
            self._finished = True
        except RequestLoopStop as ex:
            # The coroutine requests stopping the main loop.
            self._requested_stop = True
            self._result = ex.value
            self._finished = True
        except SessionEndException:
            # This is most probably the one we threw by self._coroutine.throw(value)
            # and it bubbled back to us.
            # It's fine, the coroutine can but doesn't have to handle it.
            self._finished = True
        except BaseException:
            # Any other exception is reraised, but we first need to set _finished, to ensure we
            # don't try to send or throw something to the coroutine any more.
            self._finished = True
            raise

    @property
    def result(self):
        return self._result

    @property
    def is_finished(self):
        return self._finished


class MultiHook(tf.train.SessionRunHook):
    def __init__(self, hooks=None):
        self._hooks = list(hooks or [])

    def after_create_session(self, sess, coord):
        for hook in self._hooks:
            hook.after_create_session(sess, coord)

    def begin(self):
        for hook in self._hooks:
            hook.begin()

    def before_run(self, run_context):
        fetch_dict = {}
        feed_dict = {}
        options = tf.RunOptions()

        for hook in self._hooks:
            run_args = hook.before_run(run_context)
            if run_args is None:
                continue
            fetch_dict[hook] = run_args.fetches
            if run_args.feed_dict:
                _raise_if_feeds_intersect(
                    feed_dict, run_args.feed_dict, 'Same tensor is fed by two hooks.')
                feed_dict.update(**run_args.feed_dict)
            if run_args.options:
                _merge_run_options(options, run_args.options)

        return tf.train.SessionRunArgs(fetch_dict, feed_dict=feed_dict, options=options)

    def after_run(self, run_context, run_values):
        for hook in self._hooks:
            try:
                results = run_values.results[hook]
            except (TypeError, KeyError):
                results = None

            run_values_for_hook = tf.train.SessionRunValues(
                results=results,
                run_metadata=run_values.run_metadata,
                options=run_values.options)
            hook.after_run(run_context, run_values_for_hook)

    def end(self, session):
        for hook in list(self._hooks):
            hook.end(session)


class AsyncMultiHook(MultiHook):
    def __init__(self, hooks=None):
        super().__init__(hooks)

        self._original_sys_asyncgen_hooks = sys.get_asyncgen_hooks()
        self._alive_asyncgens = weakref.WeakSet()
        self._new_hooks = collections.deque()

    def begin(self):
        # We are now in charge of finalizing asynchronous generators (see docs about
        # sys.get_asyncgen_hooks). Note: these are also called 'hooks', but of course this is
        # just a name clash. They are not the same sort of hook as the tf.train.SessionRunHook
        self._original_sys_asyncgen_hooks = sys.get_asyncgen_hooks()
        sys.set_asyncgen_hooks(
            firstiter=self._alive_asyncgens.add, finalizer=self._finalize_asyncgen)

        super().begin()
        self._update_hook_list()

    # self.before_run is inherited from MultiHook as is, there should be
    # no self._update_hook_list() there, because then new asyncgen-closing hooks
    # would get an after_run call directly after their begin().
    # But after_run should only be called when there was already also a before_run.
    # New asyncgen-closing hooks will only be active beginning in the next iteration.

    def after_run(self, run_context, run_values):
        super().after_run(run_context, run_values)
        self._update_hook_list()

    def end(self, session):
        super().end(session)
        self._update_hook_list()

        # Non-coroutine hooks are done now. Anything they wanted to do, they did in end()
        self._remove_non_coroutine_hooks()

        # However, coroutine hooks cannot proceed independently, we must loop them manually
        # so that they can make progress in parallel.
        # Therefore, if some hooks' coroutines haven't finished and would like to await more runs,
        # we let them do this by looping manually.
        self._finish_hooks(session)

        # The coroutines have finished, but there may be some asynchronous generators (asyncgens)
        # somewhere that haven't been cleaned up yet.
        # Moreover, additional asyncgens may be created when we finish these ones, so we must loop
        # here.
        while self._alive_asyncgens:
            for asyncgen in self._alive_asyncgens:
                self._finalize_asyncgen(asyncgen)
            self._update_hook_list()
            self._finish_hooks(session)

        # Now all asyncgens are finished.
        # We are ending the loop, hence we are no longer in charge of finalizing new asyncgens.
        if self._original_sys_asyncgen_hooks:
            sys.set_asyncgen_hooks(*self._original_sys_asyncgen_hooks)

    def _finish_hooks(self, session):
        run_context = tf.train.SessionRunContext((), session)
        while self._hooks:
            run_args = self.before_run(run_context)
            results = session.run(
                run_args.fetches, feed_dict=run_args.feed_dict, options=run_args.options)
            run_values = tf.train.SessionRunValues(results, run_args.options, tf.RunMetadata())
            self.after_run(run_context, run_values)  # This also cleans up any finished hooks.
            # This would be the place to check whether run_context.request_stop() was called.
            # We don't do that since we are already stopping; more cannot be done.

    def _finalize_asyncgen(self, asyncgen):
        # Generally, an asyncgen finalization function must 'schedule' the asynchronous execution
        # of asyncgen.aclose(). In case of the hook system we have here, this means adding a
        # new CoroutineHook that wraps asyncgen.aclose().
        # But we cannot add this directly to _hooks, because this method (i.e. _finalize_asyncgen)
        # may be called at any time by the garbage collector.
        # So at this particular time we may also be iterating over _hooks somewhere in the
        # MultiHook class. That would mean changing the list while iterating it, which is not
        # allowed. Therefore we just put this hook in self._new_hooks and then merge it into
        # self._hooks manually when appropriate, in the _update_hook_list() method.
        self._new_hooks.append(CoroutineHook(coroutine=asyncgen.aclose()))

    def _update_hook_list(self):
        # Add new hooks into self._hooks that were added to self._new_hooks in _finalize_asyncgen()
        # self._new_hooks may change during the loop, if the GC calls asyncgen finalization
        # (i.e. self._finalize_asyncgen), especially while inside new_hook.begin().
        # Therefore we cannot use simple iteration over self._new_hooks and need this while True and
        # popleft construct (since iterating over a changing collection is not allowed).
        try:
            while True:
                new_hook = self._new_hooks.popleft()
                new_hook.begin()
                self._hooks.append(new_hook)
        except IndexError:
            pass

        # Remove finished hooks
        for hook in list(self._hooks):
            if isinstance(hook, (CoroutineHook, _PeriodicHookWrapper)) and hook.is_finished:
                self._hooks.remove(hook)

    def _remove_non_coroutine_hooks(self):
        for hook in list(self._hooks):
            if not isinstance(hook, (CoroutineHook, _PeriodicHookWrapper)):
                self._hooks.remove(hook)


class _PeriodicHookWrapper(tf.train.SessionRunHook):
    """Decorates a SessionRunHook such that it's called only if at least `every_n_steps` steps
     and also at least `every_n_secs` seconds have passed since the end of the last run
     in which it was called.
     Naturally, if only one of `every_n_secs` and `every_n_steps` is provided, then only that
     condition is considered."""

    def __init__(self, hook, every_n_secs=None, every_n_steps=None):
        self._step = None
        self._step_timer = (tf.train.SecondOrStepTimer(every_steps=every_n_steps)
                            if every_n_steps is not None else None)
        self._second_timer = (tf.train.SecondOrStepTimer(every_secs=every_n_secs)
                              if every_n_secs is not None else None)
        self._hook = hook
        self._should_trigger = None

    @property
    def result(self):
        try:
            return self._hook.result
        except AttributeError:
            return None

    @property
    def is_finished(self):
        try:
            return self._hook.is_finished
        except AttributeError:
            return False

    def after_create_session(self, session, coord):
        self._hook.after_create_session(session, coord)

    def begin(self):
        self._step = 0
        self._hook.begin()

    def before_run(self, run_context):
        self._should_trigger = (
                (self._second_timer is None or self._second_timer.should_trigger_for_step(
                    self._step))
                and (self._step_timer is None or self._step_timer.should_trigger_for_step(
            self._step)))

        if self._should_trigger:
            return self._hook.before_run(run_context)
        else:
            return None

    def after_run(self, run_context, run_values):
        if self._should_trigger:
            self._hook.after_run(run_context, run_values)

            if self._step_timer:
                self._step_timer.update_last_triggered_step(self._step)
            if self._second_timer:
                self._second_timer.update_last_triggered_step(self._step)

        self._step += 1

    def end(self, session):
        self._hook.end(session)


# Copied over from TensorFlow 1.3.x source because it's not in the public API and may change there.
# Renamed _HookedSession to HookWrappedExistingSession to make things clearer.
# Also: added __enter__ and __exit__ to HookedSession that call begin and end on the hooks
# respectively, but it does not do anything else (e.g. the underlying session is not closed)
class WrappedSession(object):
    """Wrapper around a `tf.Session`.

    This wrapper is used as a base class for various session wrappers
    that provide additional functionality such as monitoring, coordination,
    and recovery.

    In addition to the methods exported by `SessionInterface` the wrapper
    provides a method to check for stop and never raises exceptions from
    calls to `close()`.
    """

    def __init__(self, sess):
        """Creates a `_WrappedSession`.

        Args:
          sess: A `tf.Session` or `_WrappedSession` object.  The wrapped session.
        """
        self._sess = sess
        self._wrapped_is_stoppable = isinstance(self._sess, WrappedSession)

    @property
    def graph(self):
        return self._sess.graph

    @property
    def sess_str(self):
        return self._sess.sess_str

    def should_stop(self):
        """Return true if this session should not be used anymore.

        Always return True if the session was closed.

        Returns:
          True if the session should stop, False otherwise.
        """
        if self._check_stop():
            return True
        if self._sess:
            return self._wrapped_is_stoppable and self._sess.should_stop()
        return True

    def _check_stop(self):
        """Hook for subclasses to provide their own stop condition.

        Returns:
          True if the session should stop, False otherwise.
        """
        return False

    def close(self):
        if self._sess:
            try:
                self._sess.close()
            except (tf.errors.AbortedError, tf.errors.UnavailableError):
                pass
            finally:
                self._sess = None

    def run(self, *args, **kwargs):
        return self._sess.run(*args, **kwargs)


class _HookWrappedExistingSession(WrappedSession):
    """A WrappedSession that calls hooks during calls to run().

    The list of hooks to call is passed in the constructor.  Before each call
    to `run()` the session calls the `before_run()` method of the hooks, which
    can return additional ops or tensors to run.  These are added to the arguments
    of the call to `run()`.

    When the `run()` call finishes, the session calls the `after_run()` methods of
    the hooks, passing the values returned by the `run()` call corresponding to
    the ops and tensors that each hook requested.

    If any call to the hooks, requests stop via run_context the session will be
    marked as needing to stop and its `should_stop()` method will now return
    `True`.
    """

    def __init__(self, sess, hooks):
        """Initializes a _HookedSession object.

        Args:
          sess: A `tf.Session` or a `_WrappedSession` object.
          hooks: An iterable of `SessionRunHook' objects.
        """

        WrappedSession.__init__(self, sess)
        self._hooks = hooks or []
        self._should_stop = False

    def _check_stop(self):
        """See base class."""
        return self._should_stop

    def __enter__(self):
        for hook in self._hooks:
            hook.begin()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for hook in self._hooks:
            hook.end(self._sess)
        return self

    def run(self, fetches, feed_dict=None, options=None, run_metadata=None):
        """See base class."""
        if self.should_stop():
            raise RuntimeError('Run called even after should_stop requested.')

        actual_fetches = {'caller': fetches}

        run_context = tf.train.SessionRunContext(
            original_args=tf.train.SessionRunArgs(fetches, feed_dict),
            session=self._sess)

        options = options or tf.RunOptions()
        feed_dict = self._call_hook_before_run(run_context, actual_fetches,
                                               feed_dict, options)

        # Do session run.
        run_metadata = run_metadata or tf.RunMetadata()
        outputs = WrappedSession.run(self,
                                     fetches=actual_fetches,
                                     feed_dict=feed_dict,
                                     options=options,
                                     run_metadata=run_metadata)

        for hook in self._hooks:
            hook.after_run(
                run_context,
                tf.train.SessionRunValues(
                    results=outputs[hook] if hook in outputs else None,
                    options=options,
                    run_metadata=run_metadata))
        self._should_stop = self._should_stop or run_context.stop_requested

        return outputs['caller']

    def _call_hook_before_run(self, run_context, fetch_dict, user_feed_dict, options):
        """Calls hooks.before_run and handles requests from hooks."""
        hook_feeds = {}
        for hook in self._hooks:
            request = hook.before_run(run_context)
            if request is not None:
                if request.fetches is not None:
                    fetch_dict[hook] = request.fetches
                if request.feed_dict:
                    _raise_if_feeds_intersect(
                        hook_feeds, request.feed_dict,
                        'Same tensor is fed by two hooks.')
                    hook_feeds.update(request.feed_dict)
                if request.options:
                    _merge_run_options(options, request.options)

        if not hook_feeds:
            return user_feed_dict

        if not user_feed_dict:
            return hook_feeds

        _raise_if_feeds_intersect(
            user_feed_dict, hook_feeds,
            'Same tensor is fed by a SessionRunHook and user.')
        hook_feeds.update(user_feed_dict)
        return hook_feeds


def _raise_if_feeds_intersect(feeds1, feeds2, message):
    intersection = set(feeds1.keys()) & set(feeds2.keys())
    if intersection:
        raise RuntimeError(message + ' Conflict(s): ' + str(list(intersection)))


def _merge_run_options(options, incoming_options):
    """Merge two instances of RunOptions into the first one.

    During the merger, the numerical fields including trace_level,
    timeout_in_ms, inter_op_thread_pool are set to the larger one of the two.
    The boolean value is set to the logical OR of the two.
    debug_tensor_watch_opts of the original options is extended with that from
    the incoming one.

    Args:
      options: The options to merge into.
      incoming_options: The options to be merged into the first argument.
    """
    options.trace_level = max(options.trace_level, incoming_options.trace_level)
    options.timeout_in_ms = max(options.timeout_in_ms,
                                incoming_options.timeout_in_ms)
    options.inter_op_thread_pool = max(options.inter_op_thread_pool,
                                       incoming_options.inter_op_thread_pool)
    options.output_partition_graphs = max(
        options.output_partition_graphs,
        incoming_options.output_partition_graphs)

    options.debug_options.debug_tensor_watch_opts.extend(
        incoming_options.debug_options.debug_tensor_watch_opts)
