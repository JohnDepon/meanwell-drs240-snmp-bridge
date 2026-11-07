"""A throwaway SMTP server for the tests.

The stdlib `smtpd` module was removed in Python 3.12, and this needs to run on
whatever the Pi has, so it speaks the handful of verbs smtplib uses directly.
That also means the transport is tested over a real socket rather than mocked.
"""

import socket
import threading


class SmtpSink:
    def __init__(self, host="127.0.0.1", port=0, fail_after_greeting=False):
        self.messages = []          # list of (mailfrom, [rcpt], raw_bytes)
        self.fail_after_greeting = fail_after_greeting
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,),
                             daemon=True).start()

    def _session(self, conn):
        conn.settimeout(10)
        stream = conn.makefile("rwb")

        def reply(text):
            stream.write((text + "\r\n").encode())
            stream.flush()

        try:
            reply("220 sink ESMTP")
            mailfrom, rcpttos = None, []
            while True:
                line = stream.readline()
                if not line:
                    return
                command = line.decode("utf-8", "replace").strip()
                upper = command.upper()

                if upper.startswith("EHLO"):
                    if self.fail_after_greeting:
                        reply("421 go away")
                        return
                    reply("250-sink")
                    reply("250 8BITMIME")
                elif upper.startswith("HELO"):
                    reply("250 sink")
                elif upper.startswith("MAIL FROM:"):
                    mailfrom = command[10:].strip().strip("<>")
                    reply("250 OK")
                elif upper.startswith("RCPT TO:"):
                    rcpttos.append(command[8:].strip().strip("<>"))
                    reply("250 OK")
                elif upper == "DATA":
                    reply("354 send it")
                    chunks = []
                    while True:
                        chunk = stream.readline()
                        if not chunk or chunk in (b".\r\n", b".\n"):
                            break
                        if chunk.startswith(b".."):
                            chunk = chunk[1:]
                        chunks.append(chunk)
                    self.messages.append((mailfrom, list(rcpttos),
                                          b"".join(chunks)))
                    mailfrom, rcpttos = None, []
                    reply("250 queued")
                elif upper == "RSET":
                    mailfrom, rcpttos = None, []
                    reply("250 OK")
                elif upper == "QUIT":
                    reply("221 bye")
                    return
                else:
                    reply("250 OK")
        except (OSError, socket.timeout):
            return
        finally:
            try:
                stream.close()
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
