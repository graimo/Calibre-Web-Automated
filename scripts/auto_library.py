# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

import json
import os
import shutil
import sqlite3
import sys
import subprocess


def main():
    auto_lib = AutoLibrary()
    auto_lib.check_for_app_db()
    if auto_lib.check_for_existing_library():
        auto_lib.set_library_location()
    else: # No existing library found
        auto_lib.make_new_library()
        auto_lib.set_library_location()

    print(f"[cwa-auto-library] Library location successfully set to: {auto_lib.lib_path}")
    sys.exit(0)


class AutoLibrary:
    def __init__(self):
        self.config_dir = "/config"
        self.library_dir = "/calibre-library"
        self.dirs_path = "/app/calibre-web-automated/dirs.json"
        self.app_db = "/config/app.db"

        self.empty_appdb = "/app/calibre-web-automated/empty_library/app.db"
        self.empty_metadb = "/app/calibre-web-automated/empty_library/metadata.db"

        self.metadb_path = None
        self.lib_path = None

    @property #getter
    def metadb_path(self):
        return self._metadb_path

    @metadb_path.setter
    def metadb_path(self, path):
        if path is None:
            self._metadb_path = None
            self.lib_path = None
        else:
            self._metadb_path = path
            self.lib_path = os.path.dirname(path)

    # Checks config_dir for an existing app.db, if one doesn't already exist it copies an empty one from /app/calibre-web-automated/empty_library/app.db and sets the permissions
    def check_for_app_db(self):
        files_in_config = [os.path.join(dirpath,f) for (dirpath, dirnames, filenames) in os.walk(self.config_dir) for f in filenames]
        db_files = [f for f in files_in_config if "app.db" in f]
        if len(db_files) == 0:
            print(f"[cwa-auto-library] No app.db found in {self.config_dir}, copying from /app/calibre-web-automated/empty_library/app.db")
            shutil.copyfile(self.empty_appdb, f"{self.config_dir}/app.db")
            try:
                nsm = os.getenv("NETWORK_SHARE_MODE", "false").strip().lower() in ("1", "true", "yes", "on")
                if not nsm:
                    subprocess.run(["chown", "-R", "abc:abc", self.config_dir], check=True)
                else:
                    print(f"[cwa-auto-library] NETWORK_SHARE_MODE=true detected; skipping chown of {self.config_dir}", flush=True)
            except subprocess.CalledProcessError as e:
                print(f"[cwa-auto-library] An error occurred while attempting to recursively set ownership of {self.config_dir} to abc:abc. See the following error:\n{e}", flush=True)
            print(f"[cwa-auto-library] app.db successfully copied to {self.config_dir}")
        else:
            return

    # Check for a metadata.db file in the given library dir and returns False if one doesn't exist
    # and True if one does exist, while also updating metadb_path to the path of the found metadata.db file
    # In the case of multiple metadata.db files, the user is notified and the one with the largest filesize is chosen
    def check_for_existing_library(self) -> bool: 
        files_in_library = [os.path.join(dirpath,f) for (dirpath, dirnames, filenames) in os.walk(self.library_dir) for f in filenames]
        # Consider metadata.db files across subfolders, but ignore SQLite sidecars created by WAL/journal modes
        db_files = []
        for f in files_in_library:
            base = os.path.basename(f)
            if "metadata.db" in base and not (base.endswith("-wal") or base.endswith("-shm") or base.endswith("-journal")):
                db_files.append(f)
        if len(db_files) == 1:
            self.metadb_path = db_files[0]
            print(f"[cwa-auto-library]: Existing library found at {self.lib_path}, mounting now...")
            return True
        elif len(db_files) > 1:
            print("[cwa-auto-library]: Multiple metadata.db files found in library directory:\n")
            for db in db_files:
                print(f"    - {db} | Size: {os.path.getsize(db)}")
            db_sizes = [os.path.getsize(f) for f in db_files]
            index_of_biggest_db = max(range(len(db_sizes)), key=db_sizes.__getitem__)
            self.metadb_path = db_files[index_of_biggest_db]
            print(f"\n[cwa-auto-library]: Automatically mounting the largest database using the following db file - {db_files[index_of_biggest_db]} ...")
            print("\n[cwa-auto-library]: If this is unwanted, please ensure only 1 metadata.db file / only your desired Calibre Database exists in '/calibre-library', then restart the container")
            return True
        else:
            return False

    # Sets the library's location in both dirs.json and the CW db
    def set_library_location(self):
        if self.metadb_path is not None and os.path.exists(self.metadb_path):
            self.update_dirs_json()
            self.update_calibre_web_db()
            self.ensure_owner_column()
            return
        else:
            print("[cwa-auto-library]: ERROR: metadata.db found but not mounted")
            sys.exit(1)

    # Ensures the multi-value TEXT custom column '#owner' exists in metadata.db.
    # This backs the per-user multi-owner visibility model (approach B): each book
    # stores the integer user IDs (ub.User.id) of its owners, and cps.db.common_filters
    # restricts a non-admin user to books whose #owner contains their id.
    #
    # Runs on every boot, BEFORE the web app opens its CalibreDB session, so the new
    # column is picked up by cps.db.setup_db_cc_classes without a live reconnect.
    # Idempotent (a no-op once the column exists) and best-effort (never blocks boot).
    # Uses raw SQL matching Calibre's own DDL for a normalized multi-value text column,
    # so it needs neither the Calibre binaries nor calibre-debug (which crashes on some
    # older kernels).
    def ensure_owner_column(self):
        try:
            con = sqlite3.connect(self.metadb_path, timeout=30)  # type: ignore
        except Exception as e:
            print(f"[cwa-auto-library] WARN: could not open metadata.db to ensure #owner column: {e}", flush=True)
            return
        try:
            cur = con.cursor()
            existing = cur.execute(
                "SELECT id FROM custom_columns WHERE label = 'owner'"
            ).fetchone()
            if existing is not None:
                n = existing[0]
                table = cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                    (f"custom_column_{n}",),
                ).fetchone()
                if table is not None:
                    return  # fully present -> nothing to do
                # Broken partial state (column row without its backing table): drop
                # the orphaned row and recreate cleanly below.
                print("[cwa-auto-library] WARN: #owner column row exists without its table; recreating.", flush=True)
                cur.execute("DELETE FROM custom_columns WHERE id = ?", (n,))
                con.commit()
            cur.execute(
                "INSERT INTO custom_columns "
                "(label, name, datatype, mark_for_delete, editable, display, is_multiple, normalized) "
                "VALUES ('owner', 'Owners', 'text', 0, 1, '{}', 1, 1)"
            )
            n = cur.lastrowid
            con.commit()  # persist the column row before the (auto-committing) DDL
            try:
                cur.executescript(self._owner_column_ddl(n))
                con.commit()
            except Exception:
                # Roll back to a clean slate so a later boot can retry rather than
                # leaving a half-created column that would break the web app.
                try:
                    con.rollback()
                    cur.executescript(
                        f"DROP TABLE IF EXISTS books_custom_column_{n}_link;"
                        f"DROP TABLE IF EXISTS custom_column_{n};"
                        f"DROP VIEW IF EXISTS tag_browser_custom_column_{n};"
                        f"DROP VIEW IF EXISTS tag_browser_filtered_custom_column_{n};"
                    )
                    cur.execute("DELETE FROM custom_columns WHERE id = ?", (n,))
                    con.commit()
                except Exception:
                    pass
                raise
            print(f"[cwa-auto-library]: Created multi-value custom column #owner (id={n}) for per-user ownership.", flush=True)
        except Exception as e:
            print(f"[cwa-auto-library] WARN: could not create #owner custom column: {e}", flush=True)
        finally:
            con.close()

    # DDL for a normalized (tags-like) multi-value text custom column, verbatim from
    # Calibre's own output for `calibredb add_custom_column --is-multiple`. {n} is the
    # new custom_columns.id. The 'OF author' trigger clause is a genuine Calibre artifact
    # (the link table has no author column, so it never fires) kept for byte parity.
    @staticmethod
    def _owner_column_ddl(n: int) -> str:
        return f"""
CREATE TABLE custom_column_{n}(
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    value TEXT NOT NULL COLLATE NOCASE,
    link  TEXT NOT NULL DEFAULT "",
    UNIQUE(value));
CREATE INDEX custom_column_{n}_idx ON custom_column_{n} (value COLLATE NOCASE);

CREATE TABLE books_custom_column_{n}_link(
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    book  INTEGER NOT NULL,
    value INTEGER NOT NULL,
    UNIQUE(book, value));
CREATE INDEX books_custom_column_{n}_link_aidx ON books_custom_column_{n}_link (value);
CREATE INDEX books_custom_column_{n}_link_bidx ON books_custom_column_{n}_link (book);

CREATE TRIGGER fkc_update_books_custom_column_{n}_link_a
        BEFORE UPDATE OF book ON books_custom_column_{n}_link
        BEGIN
            SELECT CASE
                WHEN (SELECT id from books WHERE id=NEW.book) IS NULL
                THEN RAISE(ABORT, 'Foreign key violation: book not in books')
            END;
        END;
CREATE TRIGGER fkc_update_books_custom_column_{n}_link_b
        BEFORE UPDATE OF author ON books_custom_column_{n}_link
        BEGIN
            SELECT CASE
                WHEN (SELECT id from custom_column_{n} WHERE id=NEW.value) IS NULL
                THEN RAISE(ABORT, 'Foreign key violation: value not in custom_column_{n}')
            END;
        END;
CREATE TRIGGER fkc_insert_books_custom_column_{n}_link
        BEFORE INSERT ON books_custom_column_{n}_link
        BEGIN
            SELECT CASE
                WHEN (SELECT id from books WHERE id=NEW.book) IS NULL
                THEN RAISE(ABORT, 'Foreign key violation: book not in books')
                WHEN (SELECT id from custom_column_{n} WHERE id=NEW.value) IS NULL
                THEN RAISE(ABORT, 'Foreign key violation: value not in custom_column_{n}')
            END;
        END;
CREATE TRIGGER fkc_delete_books_custom_column_{n}_link
        AFTER DELETE ON custom_column_{n}
        BEGIN
            DELETE FROM books_custom_column_{n}_link WHERE value=OLD.id;
        END;

CREATE VIEW tag_browser_custom_column_{n} AS SELECT
    id, value,
    (SELECT COUNT(id) FROM books_custom_column_{n}_link WHERE value=custom_column_{n}.id) count,
    (SELECT AVG(r.rating) FROM books_custom_column_{n}_link, books_ratings_link as bl, ratings as r
     WHERE books_custom_column_{n}_link.value=custom_column_{n}.id and bl.book=books_custom_column_{n}_link.book
       and r.id = bl.rating and r.rating <> 0) avg_rating,
    value AS sort
    FROM custom_column_{n};
CREATE VIEW tag_browser_filtered_custom_column_{n} AS SELECT
    id, value,
    (SELECT COUNT(books_custom_column_{n}_link.id) FROM books_custom_column_{n}_link
       WHERE value=custom_column_{n}.id AND books_list_filter(book)) count,
    (SELECT AVG(r.rating) FROM books_custom_column_{n}_link, books_ratings_link as bl, ratings as r
     WHERE books_custom_column_{n}_link.value=custom_column_{n}.id AND bl.book=books_custom_column_{n}_link.book
       AND r.id = bl.rating AND r.rating <> 0 AND books_list_filter(bl.book)) avg_rating,
    value AS sort
    FROM custom_column_{n};
"""

    # Uses sql to update CW's app.db with the correct library location (config_calibre_dir in the settings table)
    def update_calibre_web_db(self):
        if os.path.exists(self.metadb_path): # type: ignore
            try:
                print("[cwa-auto-library]: Updating Settings Database with library location...")
                con = sqlite3.connect(self.app_db, timeout=30)
                cur = con.cursor()
                cur.execute(f'UPDATE settings SET config_calibre_dir="{self.lib_path}";')
                con.commit()
                return
            except Exception as e:
                print("[cwa-auto-library]: ERROR: Could not update Calibre Web Database")
                print(e)
                sys.exit(1)
        else:
            print(f"[cwa-auto-library]: ERROR: app.db in {self.app_db} not found")
            sys.exit(1)

    # Update the dirs.json file with the new library location (lib_path))
    def update_dirs_json(self):
        """Updates the location of the calibre library stored in dirs.json with the found library"""
        try:
            print("[cwa-auto-library] Updating dirs.json with new library location...")
            with open(self.dirs_path) as f:
                dirs = json.load(f)
            dirs["calibre_library_dir"] = self.lib_path
            with open(self.dirs_path, 'w') as f:
                json.dump(dirs, f, indent=4)
            return
        except Exception as e:
            print("[cwa-auto-library]: ERROR: Could not update dirs.json")
            print(e)
            sys.exit(1)

    # Uses the empty metadata.db in /app/calibre-web-automated to create a new library
    def make_new_library(self):
        print("[cwa-auto-library]: No existing library found. Creating new library...")
        shutil.copyfile(self.empty_metadb, f"{self.library_dir}/metadata.db")
        try:
            nsm = os.getenv("NETWORK_SHARE_MODE", "false").strip().lower() in ("1", "true", "yes", "on")
            if not nsm:
                subprocess.run(["chown", "-R", "abc:abc", self.library_dir], check=True)
            else:
                print(f"[cwa-auto-library] NETWORK_SHARE_MODE=true detected; skipping chown of {self.library_dir}", flush=True)
        except subprocess.CalledProcessError as e:
            print(f"[cwa-auto-library] An error occurred while attempting to recursively set ownership of {self.library_dir} to abc:abc. See the following error:\n{e}", flush=True)
        self.metadb_path = f"{self.library_dir}/metadata.db"
        return


if __name__ == '__main__':
    main()