"""Receive one small hook packet per same-user local connection."""
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer

from antigravity import FRAME_LIMIT, Tasks, pipe_name


class AntigravityBridge(QObject):
    event = Signal(object)

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.tasks = Tasks()
        self.name = pipe_name(profile)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.setMaxPendingConnections(16)
        self.server.newConnection.connect(self.incoming)
        self.clients = set()

    def start(self):
        return self.server.isListening() or self.server.listen(self.name)

    def disconnect_tasks(self):
        for event in self.tasks.disconnect():
            self.event.emit(event)

    def close(self):
        self.server.close()
        for client in tuple(self.clients):
            self.finish(client)
        self.disconnect_tasks()

    def incoming(self):
        while self.server.hasPendingConnections():
            client = self.server.nextPendingConnection()
            if len(self.clients) >= 16:
                client.abort()
                client.deleteLater()
                continue
            self.clients.add(client)
            client.setReadBufferSize(FRAME_LIMIT + 1)
            client.readyRead.connect(lambda client=client: self.read(client))
            client.disconnected.connect(lambda client=client: self.finish(client))
            QTimer.singleShot(500, client, lambda client=client: self.finish(client))
            self.read(client)

    def read(self, client):
        if client not in self.clients:
            return
        if client.canReadLine():
            frame = bytes(client.readLine(FRAME_LIMIT + 1))
            self.finish(client)
            if len(frame) <= FRAME_LIMIT and frame.endswith(b'\n'):
                for event in self.tasks.accept(frame):
                    self.event.emit(event)
        elif client.bytesAvailable() > FRAME_LIMIT:
            self.finish(client)

    def finish(self, client):
        if client in self.clients:
            self.clients.discard(client)
            client.abort()
            client.deleteLater()
