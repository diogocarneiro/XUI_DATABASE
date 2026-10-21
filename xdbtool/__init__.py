# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""XUI DB Tool — inspect, back up, repair and migrate IPTV panel databases.

Supports XUI.one, Xtream-UI and StreamCreed MySQL/MariaDB schemas, both from a
live connection and from offline ``mysqldump`` ``.sql`` files.

The tool is designed for administrators working on databases they own or are
authorised to manage. It migrates stored credentials verbatim (hashes are not
decrypted or cracked).
"""

__version__ = "0.6.0"
__all__ = ["__version__"]
