# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Setuptools build helpers for runtime package data."""

from io import StringIO
from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from setuptools.command.build_py import build_py


class CompileTranslations(build_py):
    """Compile every messages.po into the wheel's build directory."""

    def _catalog_pairs(self):
        source_root = Path(self.get_package_dir("cps")) / "translations"
        build_root = Path(self.build_lib) / "cps" / "translations"
        for source in source_root.glob("*/LC_MESSAGES/messages.po"):
            relative = source.relative_to(source_root).with_suffix(".mo")
            yield source, build_root / relative

    def run(self):
        super().run()
        for source, destination in self._catalog_pairs():
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_text = source.read_text(encoding="utf-8").replace(
                '"PO-Revision-Date: \\n"',
                '"PO-Revision-Date: 1970-01-01 00:00+0000\\n"',
            )
            catalog = read_po(StringIO(source_text))
            with destination.open("wb") as destination_file:
                write_mo(destination_file, catalog)

    def get_outputs(self, include_bytecode=True):
        outputs = super().get_outputs(include_bytecode=include_bytecode)
        return outputs + [str(destination) for _source, destination in self._catalog_pairs()]
