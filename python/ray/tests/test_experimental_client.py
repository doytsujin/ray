import pytest
import time
import sys
import logging

from ray.experimental.client.common import ClientObjectRef
from ray.experimental.client.ray_client_helpers import ray_start_client_server


def test_real_ray_fallback(ray_start_regular_shared):
    with ray_start_client_server() as ray:

        @ray.remote
        def get_nodes_real():
            import ray as real_ray
            return real_ray.nodes()

        nodes = ray.get(get_nodes_real.remote())
        assert len(nodes) == 1, nodes

        @ray.remote
        def get_nodes():
            # Can access the full Ray API in remote methods.
            return ray.nodes()

        nodes = ray.get(get_nodes.remote())
        assert len(nodes) == 1, nodes


def test_nested_function(ray_start_regular_shared):
    with ray_start_client_server() as ray:

        @ray.remote
        def g():
            @ray.remote
            def f():
                return "OK"

            return ray.get(f.remote())

        assert ray.get(g.remote()) == "OK"


def test_put_get(ray_start_regular_shared):
    with ray_start_client_server() as ray:
        objectref = ray.put("hello world")
        print(objectref)

        retval = ray.get(objectref)
        assert retval == "hello world"


def test_wait(ray_start_regular_shared):
    with ray_start_client_server() as ray:
        objectref = ray.put("hello world")
        ready, remaining = ray.wait([objectref])
        assert remaining == []
        retval = ray.get(ready[0])
        assert retval == "hello world"

        objectref2 = ray.put(5)
        ready, remaining = ray.wait([objectref, objectref2])
        assert (ready, remaining) == ([objectref], [objectref2]) or \
            (ready, remaining) == ([objectref2], [objectref])
        ready_retval = ray.get(ready[0])
        remaining_retval = ray.get(remaining[0])
        assert (ready_retval, remaining_retval) == ("hello world", 5) \
            or (ready_retval, remaining_retval) == (5, "hello world")

        with pytest.raises(Exception):
            # Reference not in the object store.
            ray.wait([ClientObjectRef("blabla")])
        with pytest.raises(TypeError):
            ray.wait("blabla")
        with pytest.raises(TypeError):
            ray.wait(ClientObjectRef("blabla"))
        with pytest.raises(TypeError):
            ray.wait(["blabla"])


def test_remote_functions(ray_start_regular_shared):
    with ray_start_client_server() as ray:

        @ray.remote
        def plus2(x):
            return x + 2

        @ray.remote
        def fact(x):
            print(x, type(fact))
            if x <= 0:
                return 1
            # This hits the "nested tasks" issue
            # https://github.com/ray-project/ray/issues/3644
            # So we're on the right track!
            return ray.get(fact.remote(x - 1)) * x

        ref2 = plus2.remote(234)
        # `236`
        assert ray.get(ref2) == 236

        ref3 = fact.remote(20)
        # `2432902008176640000`
        assert ray.get(ref3) == 2_432_902_008_176_640_000

        # Reuse the cached ClientRemoteFunc object
        ref4 = fact.remote(5)
        assert ray.get(ref4) == 120

        # Test ray.wait()
        ref5 = fact.remote(10)
        # should return ref2, ref3, ref4
        res = ray.wait([ref5, ref2, ref3, ref4], num_returns=3)
        assert [ref2, ref3, ref4] == res[0]
        assert [ref5] == res[1]
        assert ray.get(res[0]) == [236, 2_432_902_008_176_640_000, 120]
        # should return ref2, ref3, ref4, ref5
        res = ray.wait([ref2, ref3, ref4, ref5], num_returns=4)
        assert [ref2, ref3, ref4, ref5] == res[0]
        assert [] == res[1]
        all_vals = ray.get(res[0])
        assert all_vals == [236, 2_432_902_008_176_640_000, 120, 3628800]


def test_function_calling_function(ray_start_regular_shared):
    with ray_start_client_server() as ray:

        @ray.remote
        def g():
            return "OK"

        @ray.remote
        def f():
            print(f, g)
            return ray.get(g.remote())

        print(f, type(f))
        assert ray.get(f.remote()) == "OK"


def test_basic_actor(ray_start_regular_shared):
    with ray_start_client_server() as ray:

        @ray.remote
        class HelloActor:
            def __init__(self):
                self.count = 0

            def say_hello(self, whom):
                self.count += 1
                return ("Hello " + whom, self.count)

        actor = HelloActor.remote()
        s, count = ray.get(actor.say_hello.remote("you"))
        assert s == "Hello you"
        assert count == 1
        s, count = ray.get(actor.say_hello.remote("world"))
        assert s == "Hello world"
        assert count == 2


def test_pass_handles(ray_start_regular_shared):
    """
    Test that passing client handles to actors and functions to remote actors
    in functions (on the server or raylet side) works transparently to the
    caller.
    """
    with ray_start_client_server() as ray:

        @ray.remote
        class ExecActor:
            def exec(self, f, x):
                return ray.get(f.remote(x))

            def exec_exec(self, actor, f, x):
                return ray.get(actor.exec.remote(f, x))

        @ray.remote
        def fact(x):
            out = 1
            while x > 0:
                out = out * x
                x -= 1
            return out

        @ray.remote
        def func_exec(f, x):
            return ray.get(f.remote(x))

        @ray.remote
        def func_actor_exec(actor, f, x):
            return ray.get(actor.exec.remote(f, x))

        @ray.remote
        def sneaky_func_exec(obj, x):
            return ray.get(obj["f"].remote(x))

        @ray.remote
        def sneaky_actor_exec(obj, x):
            return ray.get(obj["actor"].exec.remote(obj["f"], x))

        def local_fact(x):
            if x <= 0:
                return 1
            return x * local_fact(x - 1)

        assert ray.get(fact.remote(7)) == local_fact(7)
        assert ray.get(func_exec.remote(fact, 8)) == local_fact(8)
        test_obj = {}
        test_obj["f"] = fact
        assert ray.get(sneaky_func_exec.remote(test_obj, 5)) == local_fact(5)
        actor_handle = ExecActor.remote()
        assert ray.get(actor_handle.exec.remote(fact, 7)) == local_fact(7)
        assert ray.get(func_actor_exec.remote(actor_handle, fact,
                                              10)) == local_fact(10)
        second_actor = ExecActor.remote()
        assert ray.get(actor_handle.exec_exec.remote(second_actor, fact,
                                                     9)) == local_fact(9)
        test_actor_obj = {}
        test_actor_obj["actor"] = second_actor
        test_actor_obj["f"] = fact
        assert ray.get(sneaky_actor_exec.remote(test_actor_obj,
                                                4)) == local_fact(4)


def test_basic_log_stream(ray_start_regular_shared):
    with ray_start_client_server() as ray:
        log_msgs = []

        def test_log(level, msg):
            log_msgs.append(msg)

        ray.worker.log_client.log = test_log
        ray.worker.log_client.set_logstream_level(logging.DEBUG)
        # Allow some time to propogate
        time.sleep(1)
        x = ray.put("Foo")
        assert ray.get(x) == "Foo"
        time.sleep(1)
        logs_with_id = [msg for msg in log_msgs if msg.find(x.id.hex()) >= 0]
        assert len(logs_with_id) >= 2
        assert any((msg.find("get") >= 0 for msg in logs_with_id))
        assert any((msg.find("put") >= 0 for msg in logs_with_id))


def test_stdout_log_stream(ray_start_regular_shared):
    with ray_start_client_server() as ray:
        log_msgs = []

        def test_log(level, msg):
            log_msgs.append(msg)

        ray.worker.log_client.stdstream = test_log

        @ray.remote
        def print_on_stderr_and_stdout(s):
            print(s)
            print(s, file=sys.stderr)

        time.sleep(1)
        print_on_stderr_and_stdout.remote("Hello world")
        time.sleep(1)
        assert len(log_msgs) == 2
        assert all((msg.find("Hello world") for msg in log_msgs))


def test_basic_named_actor(ray_start_regular_shared):
    """
    Test that ray.get_actor() can create and return a detached actor.
    """
    with ray_start_client_server() as ray:

        @ray.remote
        class Accumulator:
            def __init__(self):
                self.x = 0

            def inc(self):
                self.x += 1

            def get(self):
                return self.x

        # Create the actor
        actor = Accumulator.options(name="test_acc").remote()

        actor.inc.remote()
        actor.inc.remote()
        del actor

        new_actor = ray.get_actor("test_acc")
        new_actor.inc.remote()
        assert ray.get(new_actor.get.remote()) == 3


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
