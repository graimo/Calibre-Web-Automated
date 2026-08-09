# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Compatibility import for installations that do not use the Docker path."""

from scripts.cwa_db import CWA_DB

__all__ = ["CWA_DB"]
