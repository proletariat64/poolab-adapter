"""Exercise a sender blocked on credits when remote stdin closes or the child exits."""
import base64
import threading
import time
import unittest

from experiments.transport_probe import Channel


class LocalChannelTests(unittest.TestCase):
    def blocked_channel(self, wire):
        channel = Channel.__new__(Channel)
        channel.prefix = b"PA1:test:"
        channel.buffer = bytearray(wire)
        channel.flow_control = True
        channel.closed = threading.Event()
        channel.input_stopped = threading.Event()
        channel.stdin_closed_by_peer = False
        channel.credit_condition = threading.Condition()
        channel.input_credit = 0
        return channel

    def test_stdin_close_and_exit_wake_a_sender_waiting_for_credits(self):
        for terminal in (b"N", b"X"):
            with self.subTest(terminal=terminal):
                wire = (b"PA1:test:N:\nPA1:test:D:b3V0\n" if terminal == b"N"
                        else b"PA1:test:X:0\n")
                channel = self.blocked_channel(wire)
                stopped = threading.Event()

                def sender(channel=channel, stopped=stopped):
                    try:
                        channel.send(b"D", base64.b64encode(b"input"))
                    except BrokenPipeError:
                        stopped.set()

                thread = threading.Thread(target=sender, daemon=True)
                thread.start()
                try:
                    kind, _ = channel.frame(time.monotonic() + 1)
                    self.assertEqual(kind, b"D" if terminal == b"N" else b"X")
                    self.assertTrue(stopped.wait(1), "remote closure stranded the credit waiter")
                finally:
                    with channel.credit_condition:
                        channel.input_stopped.set()
                        channel.credit_condition.notify_all()
                    thread.join(timeout=1)
                self.assertFalse(thread.is_alive())

    def test_invalid_credit_count_is_rejected(self):
        channel = self.blocked_channel(b"PA1:test:C:I:not-a-number\n")
        with self.assertRaisesRegex(ValueError, "invalid stdin credit count"):
            channel.frame(time.monotonic() + 1)


if __name__ == "__main__":
    unittest.main()
