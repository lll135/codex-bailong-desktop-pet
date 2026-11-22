"""Cancellable background refresh for the selected Antigravity card."""
import sqlite3

from PySide6.QtCore import QThread

from antigravity_usage import AntigravityUsage
from antigravity_quota import normalize_quota,read_quota,snapshot_path
from antigravity_sidecar import request


def query_quota(profile):
    try:return normalize_quota(request(profile))
    except (OSError,ValueError,RecursionError):pass
    return None


class AntigravityData(QThread):
    def __init__(self, index, conversations, profile, parent=None):
        super().__init__(parent)
        self.index = AntigravityUsage(index, conversations)
        self.snapshot = None
        self.profile = profile
        self.quota = None

    def run(self):
        self.quota = query_quota(self.profile) or read_quota(snapshot_path(self.profile))
        try:
            self.snapshot = self.index.refresh(self.isInterruptionRequested)
        except (OSError, sqlite3.Error, ValueError):
            self.snapshot = {'provider': 'antigravity', 'unavailable': True, 'partial': True}
