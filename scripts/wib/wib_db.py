import hashlib
import json
import os
import sqlite3
import re
import time
from shutil import copy2
from modules import scripts, shared
from tempfile import gettempdir
from PIL import Image
from contextlib import contextmanager

version = 7

path_recorder_file = os.path.join(scripts.basedir(), "path_recorder.txt")
aes_cache_file = os.path.join(scripts.basedir(), "aes_scores.json")
exif_cache_file = os.path.join(scripts.basedir(), "exif_data.json")
ranking_file = os.path.join(scripts.basedir(), "ranking.json")
archive = os.path.join(scripts.basedir(), "archive")
source_db_file = os.path.join(scripts.basedir(), "wib.sqlite3")
tmp_db_file = os.path.join(gettempdir(), "sd-images-browser.sqlite3")

db_file = source_db_file
if getattr(shared.cmd_opts, "image_browser_tmp_db", False):
    db_file = tmp_db_file
    if os.path.exists(source_db_file):
        copy2(source_db_file, tmp_db_file)
    elif os.path.exists(tmp_db_file):
        os.remove(tmp_db_file)

def backup_tmp_db():
    if(db_file == tmp_db_file):
        copy2(tmp_db_file, source_db_file)

np = "Negative prompt: "
st = "Steps: "
timeout = 60 # Timeout for locked database in seconds
max_retries = 5  # Number of retries for locked database
retry_delay = 1  # Initial delay between retries in seconds

@contextmanager
def transaction(db = db_file):
    conn = sqlite3.connect(db, timeout=timeout)
    try:
        conn.isolation_level = None
        cursor = conn.cursor()
        cursor.execute("BEGIN")
        yield cursor
        cursor.execute("COMMIT")
    finally:
        conn.close()
        backup_tmp_db()

def execute_with_retry(func, *args, **kwargs):
    """Execute a database function with retry mechanism for locked database"""
    current_retry = 0
    last_error = None
    
    while current_retry < max_retries:
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                current_retry += 1
                last_error = e
                # Exponential backoff
                sleep_time = retry_delay * (2 ** (current_retry - 1))
                print(f"Database locked, retrying in {sleep_time:.2f} seconds... (Attempt {current_retry}/{max_retries})")
                time.sleep(sleep_time)
            else:
                # If different error, re-raise it
                raise
    
    # If exhausted retries, raise last error
    raise last_error

def create_filehash(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS filehash (
            file TEXT PRIMARY KEY,
            hash TEXT,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TRIGGER filehash_tr
        AFTER UPDATE ON filehash
        BEGIN
            UPDATE filehash SET updated = CURRENT_TIMESTAMP WHERE file = OLD.file;
        END;
    ''')

    return

def create_work_files(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS work_files (
            file TEXT PRIMARY KEY
        )
    ''')

    return

def create_db(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS db_data (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS path_recorder (
            path TEXT PRIMARY KEY,
            depth INT,
            path_display TEXT,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TRIGGER path_recorder_tr
        AFTER UPDATE ON path_recorder
        BEGIN
            UPDATE path_recorder SET updated = CURRENT_TIMESTAMP WHERE path = OLD.path;
        END;
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS exif_data (
            file TEXT,
            key TEXT,
            value TEXT,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (file, key)
        )
    ''')

    cursor.execute('''
        CREATE INDEX IF NOT EXISTS exif_data_key ON exif_data (key)
    ''')

    cursor.execute('''
        CREATE TRIGGER exif_data_tr
        AFTER UPDATE ON exif_data
        BEGIN
            UPDATE exif_data SET updated = CURRENT_TIMESTAMP WHERE file = OLD.file AND key = OLD.key;
        END;
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ranking (
            file TEXT PRIMARY KEY,
            name TEXT,
            ranking TEXT,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE INDEX IF NOT EXISTS ranking_name ON ranking (name)
    ''')

    cursor.execute('''
        CREATE TRIGGER ranking_tr
        AFTER UPDATE ON ranking
        BEGIN
            UPDATE ranking SET updated = CURRENT_TIMESTAMP WHERE file = OLD.file;
        END;
    ''')

    create_filehash(cursor)
    create_work_files(cursor)

    return

def migrate_path_recorder(cursor):
    if os.path.exists(path_recorder_file):
        try:
            with open(path_recorder_file) as f:
                # json-version
                path_recorder = json.load(f)
            for path, values in path_recorder.items():
                path = os.path.realpath(path)
                depth = values["depth"]
                path_display = f"{path} [{depth}]"
                cursor.execute('''
                INSERT INTO path_recorder (path, depth, path_display)
                VALUES (?, ?, ?)
                ''', (path, depth, path_display))
        except json.JSONDecodeError:
            with open(path_recorder_file) as f:
                # old txt-version
                path = f.readline().rstrip("\n")
                while len(path) > 0:
                    path = os.path.realpath(path)
                    cursor.execute('''
                    INSERT INTO path_recorder (path, depth, path_display)
                    VALUES (?, ?, ?)
                    ''', (path, 0, f"{path} [0]"))
                    path = f.readline().rstrip("\n")

    return

def split_exif_data(info):
    prompt = "0"
    negative_prompt = "0"
    key_values = "0: 0"
    key_value_pairs = []

    def parse_value_pairs(kv_str, key_prefix=""):
        # Regular expression pattern to match key-value pairs, including multiline prompts
        pattern = r"((?:\w+ )?(?:Prompt|Negative Prompt)|[^:]+):\s*((?:[^,]+(?:,(?![^:]+:))?)+)"

        # Find all matches
        matches = re.findall(pattern, kv_str, re.IGNORECASE | re.DOTALL)
        result = {}
        current_prompt = None

        def process_prompt(key, value, current_prompt):
            if current_prompt is None:
                result[key] = value
                current_prompt = key
            else:
                pk_values = [v.strip() for v in key.split(",") if v.strip()]
                result[current_prompt] += f",{','.join(pk_values[:-1])}"
                current_prompt = pk_values[-1]
                result[current_prompt] = ",".join([v.strip() for v in value.split(",") if v.strip()])

            return current_prompt

        def process_regular_key(key, value, current_prompt):
            values = [v.strip() for v in value.split(",") if v.strip()]
            if current_prompt is not None:
                pk_values = [v.strip() for v in key.split(",") if v.strip()]
                result[current_prompt] += f",{','.join(pk_values[:-1])}"
                current_prompt = None
                key = pk_values[-1]
            result[key] = values[0] if len(values) == 1 else ",".join(values)

            return current_prompt

        for key, value in matches:
            key = key.strip(" ,")
            value = value.strip()

            if "prompt" in key.lower() or "prompt" in value.lower():
                current_prompt = process_prompt(key, value, current_prompt)
            else:
                current_prompt = process_regular_key(key, value, current_prompt)

        # Print the resulting key-value pairs
        for key, value in result.items():
            value = value.strip(" ,")
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
                parse_value_pairs(value, f"{key_prefix} - {key}" if key_prefix != "" else key)

            key_value_pairs.append((f"{key_prefix} - {key}" if key_prefix != "" else key, value))

    if info != "0":
        info_list = info.split("\n")
        prompt = ""
        negative_prompt = ""
        key_values = ""
        for info_item in info_list:
            if info_item.startswith(st):
                key_values = info_item
            elif info_item.startswith(np):
                negative_prompt = info_item.replace(np, "")
            else:
                if prompt == "":
                    prompt = info_item
                else:
                    # multiline prompts
                    prompt = f"{prompt}\n{info_item}"

    if key_values != "":
        pattern = r'(\w+(?:\s+\w+)*?):\s*((?:"[^"]*"|[^,])+)(?:,\s*|$)'
        matches = re.findall(pattern, key_values)
        result = {key.strip(): value.strip() for key, value in matches}

        # Save resulting key-value pairs
        for key, value in result.items():
            value = value.strip(" ,")
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
                parse_value_pairs(value, key)

            key_value_pairs.append((key, value))

    return prompt, negative_prompt, key_value_pairs

def update_exif_data(cursor, file, info):
    prompt, negative_prompt, key_value_pairs = split_exif_data(info)
    if key_value_pairs:
        try:
            cursor.execute('''
            INSERT INTO exif_data (file, key, value)
            VALUES (?, ?, ?)
            ''', (file, "prompt", prompt))
        except sqlite3.IntegrityError:
            # Duplicate, delete all "file" entries and try again
            cursor.execute('''
            DELETE FROM exif_data
            WHERE file = ?
            ''', (file,))

            cursor.execute('''
            INSERT INTO exif_data (file, key, value)
            VALUES (?, ?, ?)
            ''', (file, "prompt", prompt))

        cursor.execute('''
        INSERT INTO exif_data (file, key, value)
        VALUES (?, ?, ?)
        ''', (file, "negative_prompt", negative_prompt))

        for (key, value) in key_value_pairs:
            try:
                cursor.execute('''
                INSERT INTO exif_data (file, key, value)
                VALUES (?, ?, ?)
                ''', (file, key, value))
            except sqlite3.IntegrityError:
                pass

    return

def migrate_exif_data(cursor):
    if os.path.exists(exif_cache_file):
        with open(exif_cache_file, 'r') as file:
            exif_cache = json.load(file)

        for file, info in exif_cache.items():
            file = os.path.realpath(file)
            update_exif_data(cursor, file, info)

    return

def migrate_ranking(cursor):
    if os.path.exists(ranking_file):
        with open(ranking_file, 'r') as file:
            ranking = json.load(file)
        for file, info in ranking.items():
            if info != "None":
                file = os.path.realpath(file)
                name = os.path.basename(file)
                cursor.execute('''
                INSERT INTO ranking (file, name, ranking)
                VALUES (?, ?, ?)
                ''', (file, name, info))

    return

def get_hash(file):
    # Get filehash without exif info
    try:
        image = Image.open(file)
    except Exception as e:
        print(e)

    hash = hashlib.sha512(image.tobytes()).hexdigest()
    image.close()

    return hash

def migrate_filehash(cursor, version):
    if version <= "4":
        create_filehash(cursor)

    cursor.execute('''
    SELECT file
    FROM ranking
    ''')
    for (file,) in cursor.fetchall():
        if os.path.exists(file):
            hash = get_hash(file)
            cursor.execute('''
            INSERT OR REPLACE
            INTO filehash (file, hash)
            VALUES (?, ?)
            ''', (file, hash))

    return

def migrate_work_files(cursor):
    create_work_files(cursor)

    return

def update_db_data(cursor, key, value):
    cursor.execute('''
    INSERT OR REPLACE
    INTO db_data (key, value)
    VALUES (?, ?)
    ''', (key, value))

    return

def get_version():
    with transaction() as cursor:
        cursor.execute('''
        SELECT value
        FROM db_data
        WHERE key = 'version'
        ''',)
        db_version = cursor.fetchone()

    return db_version

def get_last_default_tab():
    with transaction() as cursor:
        cursor.execute('''
        SELECT value
        FROM db_data
        WHERE key = 'last_default_tab'
        ''',)
        last_default_tab = cursor.fetchone()

    return last_default_tab

def migrate_path_recorder_dirs(cursor):
    cursor.execute('''
    SELECT path, path_display
    FROM path_recorder
    ''')
    for (path, path_display) in cursor.fetchall():
        real_path = os.path.realpath(path)
        if path != real_path:
            update_from = path
            update_to = real_path
            try:
                cursor.execute('''
                UPDATE path_recorder
                SET path = ?,
                    path_display = ? || SUBSTR(path_display, LENGTH(?) + 1)
                WHERE path = ?
                ''', (update_to, update_to, update_from, update_from))
            except sqlite3.IntegrityError as e:
                # these are double keys, because the same file can be in the db with different path notations
                (e_msg,) = e.args
                if e_msg.startswith("UNIQUE constraint"):
                    cursor.execute('''
                    DELETE FROM path_recorder
                    WHERE path = ?
                    ''', (update_from,))
                else:
                    raise

    return

def migrate_exif_data_dirs(cursor):
    cursor.execute('''
    SELECT file
    FROM exif_data
    ''')
    for (filepath,) in cursor.fetchall():
        (path, file) = os.path.split(filepath)
        real_path = os.path.realpath(path)
        if path != real_path:
            update_from = filepath
            update_to = os.path.join(real_path, file)
            try:
                cursor.execute('''
                UPDATE exif_data
                SET file = ?
                WHERE file = ?
                ''', (update_to, update_from))
            except sqlite3.IntegrityError as e:
                # these are double keys, because the same file can be in the db with different path notations
                (e_msg,) = e.args
                if e_msg.startswith("UNIQUE constraint"):
                    cursor.execute('''
                    DELETE FROM exif_data
                    WHERE file = ?
                    ''', (update_from,))
                else:
                    raise

    return

def migrate_ranking_dirs(cursor, db_version):
    if db_version == "1":
        cursor.execute('''
        ALTER TABLE ranking
        ADD COLUMN name TEXT
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS ranking_name ON ranking (name)
        ''')

    cursor.execute('''
    SELECT file, ranking
    FROM ranking
    ''')
    for (filepath, ranking) in cursor.fetchall():
        if filepath == "" or ranking == "None":
            cursor.execute('''
            DELETE FROM ranking
            WHERE file = ?
            ''', (filepath,))
        else:
            (path, file) = os.path.split(filepath)
            real_path = os.path.realpath(path)
            name = file
            update_from = filepath
            update_to = os.path.join(real_path, file)
            try:
                cursor.execute('''
                UPDATE ranking
                SET file = ?,
                    name = ?
                WHERE file = ?
                ''', (update_to, name, update_from))
            except sqlite3.IntegrityError as e:
                # these are double keys, because the same file can be in the db with different path notations
                (e_msg,) = e.args
                if e_msg.startswith("UNIQUE constraint"):
                    cursor.execute('''
                    DELETE FROM ranking
                    WHERE file = ?
                    ''', (update_from,))
                else:
                    raise

    return

def check():
    if not os.path.exists(db_file):
        print("Image Browser: Creating database")
        with transaction() as cursor:
            create_db(cursor)
            update_db_data(cursor, "version", version)
            update_db_data(cursor, "last_default_tab", "Maintenance")
            migrate_path_recorder(cursor)
            migrate_exif_data(cursor)
            migrate_ranking(cursor)
            migrate_filehash(cursor, str(version))
        print("Image Browser: Database created")
    db_version = get_version()

    with transaction() as cursor:
        if db_version[0] <= "2":
            # version 1 database had mixed path notations, changed them all to abspath
            # version 2 database still had mixed path notations, because of windows short name, changed them all to realpath
            print(f"Image Browser: Upgrading database from version {db_version[0]} to version {version}")
            migrate_path_recorder_dirs(cursor)
            migrate_exif_data_dirs(cursor)
            migrate_ranking_dirs(cursor, db_version[0])
        if db_version[0] <= "4":
            migrate_filehash(cursor, db_version[0])
        if db_version[0] <= "5":
            migrate_work_files(cursor)
        if db_version[0] <= "6":
            update_db_data(cursor, "last_default_tab", "Others")

            update_db_data(cursor, "version", version)
            print(f"Image Browser: Database upgraded from version {db_version[0]} to version {version}")

    return version

def load_path_recorder():
    with transaction() as cursor:
        cursor.execute('''
        SELECT path, depth, path_display
        FROM path_recorder
        ''')
        path_recorder = {path: {"depth": depth, "path_display": path_display} for path, depth, path_display in cursor.fetchall()}

    return path_recorder

def select_ranking(file):
    with transaction() as cursor:
        cursor.execute('''
        SELECT ranking
        FROM ranking
        WHERE file = ?
        ''', (file,))
        ranking_value = cursor.fetchone()

    if ranking_value is None:
        return_ranking = "None"
    else:
        (return_ranking,) = ranking_value

    return return_ranking

def load_ranking():
    with transaction() as cursor:
        cursor.execute('''
        SELECT file, ranking
        FROM ranking
        ''')
        rows = cursor.fetchall()

    return dict(rows)

def update_ranking(file, ranking):
    name = os.path.basename(file)
    with transaction() as cursor:
        if ranking == "None":
            cursor.execute('''
            DELETE FROM ranking
            WHERE file = ?
            ''', (file,))
        else:
            cursor.execute('''
            INSERT OR REPLACE
            INTO ranking (file, name, ranking)
            VALUES (?, ?, ?)
            ''', (file, name, ranking))

            hash = get_hash(file)
            cursor.execute('''
            INSERT OR REPLACE
            INTO filehash (file, hash)
            VALUES (?, ?)
            ''', (file, hash))

    return

def update_path_recorder(path, depth, path_display):
    with transaction() as cursor:
        cursor.execute('''
        INSERT OR REPLACE
        INTO path_recorder (path, depth, path_display)
        VALUES (?, ?, ?)
        ''', (path, depth, path_display))

    return

def update_path_recorder(path, depth, path_display):
    with transaction() as cursor:
        cursor.execute('''
        INSERT OR REPLACE
        INTO path_recorder (path, depth, path_display)
        VALUES (?, ?, ?)
        ''', (path, depth, path_display))

    return

def delete_path_recorder(path):
    with transaction() as cursor:
        cursor.execute('''
        DELETE FROM path_recorder
        WHERE path = ?
        ''', (path,))

    return

def update_path_recorder_mult(cursor, update_from, update_to):
    cursor.execute('''
    UPDATE path_recorder
    SET path = ?,
        path_display = ? || SUBSTR(path_display, LENGTH(?) + 1)
    WHERE path = ?
    ''', (update_to, update_to, update_from, update_from))

    return

def update_exif_data_mult(cursor, update_from, update_to):
    update_from = update_from + os.path.sep
    update_to = update_to + os.path.sep
    cursor.execute('''
    UPDATE exif_data
    SET file = ? || SUBSTR(file, LENGTH(?) + 1)
    WHERE file like ? || '%'
    ''', (update_to, update_from, update_from))

    return

def update_ranking_mult(cursor, update_from, update_to):
    update_from = update_from + os.path.sep
    update_to = update_to + os.path.sep
    cursor.execute('''
    UPDATE ranking
    SET file = ? || SUBSTR(file, LENGTH(?) + 1)
    WHERE file like ? || '%'
    ''', (update_to, update_from, update_from))

    return

def delete_exif_0(cursor):
    cursor.execute('''
    DELETE FROM exif_data
    WHERE file IN (
        SELECT file FROM exif_data a
        WHERE value = '0'
        GROUP BY file
        HAVING COUNT(*) = (SELECT COUNT(*) FROM exif_data WHERE file = a.file)
    )
    ''')

    return

def get_ranking_by_file(cursor, file):
    cursor.execute('''
    SELECT ranking
    FROM ranking
    WHERE file = ?
    ''', (file,))
    ranking_value = cursor.fetchone()

    return ranking_value

def get_ranking_by_name(cursor, name):
    cursor.execute('''
    SELECT file, ranking
    FROM ranking
    WHERE name = ?
    ''', (name,))
    ranking_value = cursor.fetchone()

    if ranking_value is not None:
        (file, _) = ranking_value
        cursor.execute('''
        SELECT hash
        FROM filehash
        WHERE file = ?
        ''', (file,))
        hash_value = cursor.fetchone()
    else:
        hash_value = None

    return ranking_value, hash_value

def insert_ranking(cursor, file, ranking, hash):
    name = os.path.basename(file)
    cursor.execute('''
    INSERT INTO ranking (file, name, ranking)
    VALUES (?, ?, ?)
    ''', (file, name, ranking))

    cursor.execute('''
    INSERT OR REPLACE
    INTO filehash (file, hash)
    VALUES (?, ?)
    ''', (file, hash))

    return

def replace_ranking(cursor, file, alternate_file, hash):
    name = os.path.basename(file)
    cursor.execute('''
    UPDATE ranking
    SET file = ?
    WHERE file = ?
    ''', (file, alternate_file))

    cursor.execute('''
    INSERT OR REPLACE
    INTO filehash (file, hash)
    VALUES (?, ?)
    ''', (file, hash))

    return

def update_exif_data_by_key(cursor, file, key, value):
    cursor.execute('''
    INSERT OR REPLACE
    INTO exif_data (file, key, value)
    VALUES (?, ?, ?)
    ''', (file, key, value))

    return

def select_prompts(file):
    with transaction() as cursor:
        cursor.execute('''
        SELECT key, value
        FROM exif_data
        WHERE file = ?
          AND KEY in ('prompt', 'negative_prompt')
        ''', (file,))

        rows = cursor.fetchall()
    prompt = ""
    neg_prompt = ""
    for row in rows:
        (key, value) = row
        if key == 'prompt':
            prompt = value
        elif key == 'negative_prompt':
            neg_prompt = value

    return prompt, neg_prompt

def load_exif_data(exif_cache):
    # Ordered aggregates (SQLite 3.44+) avoid sorting the whole table twice
    if sqlite3.sqlite_version_info >= (3, 44, 0):
        query = '''
        SELECT file, group_concat(
            CASE WHEN key = 'prompt' OR key = 'negative_prompt'
                 THEN key || ': ' || value || char(10)
                 ELSE key || ': ' || value END,
            ', ' ORDER BY
                CASE WHEN key = 'prompt' THEN 0
                     WHEN key = 'negative_prompt' THEN 1 ELSE 2 END,
                key
        ) AS string
        FROM exif_data
        GROUP BY file
        '''
    else:
        query = '''
        SELECT file, group_concat(
            case when key = 'prompt' or key = 'negative_prompt' then key || ': ' || value || '\n'
            else key || ': ' || value
            end, ', ') AS string
        FROM (
            SELECT *
            FROM exif_data
            ORDER BY
                CASE WHEN key = 'prompt' THEN 0
                    WHEN key = 'negative_prompt' THEN 1
                    ELSE 2 END,
                key
        )
        GROUP BY file
        '''
    with transaction() as cursor:
        cursor.execute(query)
        rows = cursor.fetchall()
    for row in rows:
        exif_cache[row[0]] = row[1]

    return exif_cache

def load_exif_data_by_key(cache, key1, key2):
    with transaction() as cursor:
        cursor.execute('''
        SELECT file, value
        FROM exif_data
        WHERE key IN (?, ?)
        ''', (key1, key2))

        rows = cursor.fetchall()
    for row in rows:
        cache[row[0]] = row[1]

    return cache

def get_exif_dirs():
    with transaction() as cursor:
        cursor.execute('''
        SELECT file
        FROM exif_data
        ''')

    rows = cursor.fetchall()

    dirs = {}
    for row in rows:
        dir = os.path.dirname(row[0])
        dirs[dir] = dir

    return dirs

def fill_work_files(cursor, fileinfos):
    filenames = [x[0] for x in fileinfos]
    
    def _execute_fill():
        with transaction() as retry_cursor:
            retry_cursor.execute('''
            DELETE
            FROM work_files
            ''')
            
            retry_cursor.executemany('''
            INSERT INTO work_files (file)
            VALUES (?)
            ''', [(x,) for x in filenames])
    
    # Use the retry mechanism
    execute_with_retry(_execute_fill)
    
    return

def filter_aes(cursor, fileinfos, aes_filter_min_num, aes_filter_max_num):
    key = "aesthetic_score"

    cursor.execute('''
    DELETE
    FROM work_files
    WHERE file not in (
        SELECT file
        FROM exif_data b
        WHERE file = b.file
          AND b.key = ?
          AND CAST(b.value AS REAL) between ? and ?
    )
    ''', (key, aes_filter_min_num, aes_filter_max_num))

    cursor.execute('''
    SELECT file
    FROM work_files
    ''')

    rows = cursor.fetchall()

    fileinfos_dict = {pair[0]: pair[1] for pair in fileinfos}
    fileinfos_new = []
    for (file,) in rows:
        if fileinfos_dict.get(file) is not None:
            fileinfos_new.append((file, fileinfos_dict[file]))

    return fileinfos_new

def filter_ranking(cursor, fileinfos, ranking_filter, ranking_filter_min_num, ranking_filter_max_num):
    if ranking_filter == "None":
        cursor.execute('''
        DELETE
        FROM work_files
        WHERE file IN (
            SELECT file
            FROM ranking b
            WHERE file = b.file
        )
        ''')
    elif ranking_filter == "Min-max":
        cursor.execute('''
        DELETE
        FROM work_files
        WHERE file NOT IN (
            SELECT file
            FROM ranking b
            WHERE file = b.file
            AND b.ranking BETWEEN ? AND ?
        )
        ''', (ranking_filter_min_num, ranking_filter_max_num))
    else:
        cursor.execute('''
        DELETE
        FROM work_files
        WHERE file NOT IN (
            SELECT file
            FROM ranking b
            WHERE file = b.file
            AND b.ranking = ?
        )
        ''', (ranking_filter,))

    cursor.execute('''
    SELECT file
    FROM work_files
    ''')

    rows = cursor.fetchall()

    fileinfos_dict = {pair[0]: pair[1] for pair in fileinfos}
    fileinfos_new = []
    for (file,) in rows:
        if fileinfos_dict.get(file) is not None:
            fileinfos_new.append((file, fileinfos_dict[file]))

    return fileinfos_new

def select_x_y(cursor, file):
    cursor.execute('''
    SELECT value
    FROM exif_data
    WHERE file = ?
    AND key = 'Size'
    ''', (file,))
    size_value = cursor.fetchone()

    if size_value is None:
        x = "?"
        y = "?"
    else:
        (size,) = size_value
        parts = size.split("x")
        x = parts[0]
        y = parts[1]

    return x, y
