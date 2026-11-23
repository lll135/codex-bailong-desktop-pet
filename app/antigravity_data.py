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
        self.quota_group = ''
        self.project_range = None
        self.priority_sessions = ()

    def run(self):
        sessions = tuple(self.priority_sessions)
        quota_group = str(self.quota_group or '')
        project_range = self.project_range
        self.quota = query_quota(self.profile) or read_quota(snapshot_path(self.profile))
        try:
            self.snapshot = self.index.refresh(
                self.isInterruptionRequested,
                priority_sessions=sessions,
                quota=self.quota,
                quota_group=quota_group,
                profile=self.profile,
                calendar_range=project_range,
            )
        except (OSError, sqlite3.Error, ValueError):
            self.snapshot = {'provider': 'antigravity', 'unavailable': True, 'partial': True}
        if isinstance(self.snapshot, dict) and not self.snapshot.get('unavailable'):
            from antigravity import conversation_overview
            rate_sessions = [row['session'] for row in (self.snapshot.get('historical_rate') or {}).get('details', ())]
            targets = list(dict.fromkeys((*self.snapshot.get('recent_sessions', ()), *rate_sessions, *sessions)))
            overview = conversation_overview(self.profile, targets)
            if overview:
                titles = {
                    sid: info['title'] for sid, info in overview.items() if info.get('title')
                }
                self.snapshot['session_titles'] = titles
                for row in (self.snapshot.get('historical_rate') or {}).get('details', ()):
                    if row.get('session') in titles:
                        row['name'] = titles[row['session']]
