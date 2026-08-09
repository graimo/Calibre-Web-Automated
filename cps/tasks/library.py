# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Background tasks for managed Calibre-library provisioning."""

from flask_babel import lazy_gettext as N_

from cps import library_control, ub
from cps.services.worker import CalibreTask


class TaskProvisionPersonalLibraries(CalibreTask):
    def __init__(self, user_id=None):
        super().__init__(N_("Provisioning personal library"))
        self.user_id = int(user_id) if user_id is not None else None
        self.self_cleanup = True

    def run(self, worker_thread):
        del worker_thread
        app_session = None
        try:
            app_session = ub.init_db_thread()
            query = app_session.query(ub.User)
            if self.user_id is not None:
                query = query.filter(ub.User.id == self.user_id)
            users = query.all()
            count = len(users)
            for index, user in enumerate(users, start=1):
                library_control.provision_personal_library(user, app_session)
                self.progress = index / count if count else 1
            self._handleSuccess()
        except Exception:
            if app_session is not None:
                app_session.rollback()
            raise
        finally:
            try:
                if app_session is not None:
                    app_session.close()
            finally:
                library_control._release_provisioning_queue_key(self.user_id)

    @property
    def name(self):
        return "Provision personal libraries"

    @property
    def is_cancellable(self):
        return False
