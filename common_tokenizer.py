# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

from typing import Iterator

# Tokenizer for the config text format id Tech 3 games (Jedi Academy included) use for
# animation.cfg and similar files - matches COM_ParseExt (q_shared.c/bg_panimate.c): a `//`,
# `/* */`, or `"` is only special as the first character(s) of a new token. Once a bare token has
# started, it always runs to the next whitespace regardless of what characters occur inside it -
# comments/quotes cannot start mid-token (e.g. `foo//bar` is one token, not `foo` + a comment).
# Whitespace is any character <= ' ' (0x20), matching the engine; newlines are ordinary
# whitespace here, not a token/entry separator, so entries may span multiple lines.


def tokenize(text: str) -> Iterator[str]:
    i = 0
    n = len(text)
    while True:
        # skip whitespace and comments - a new token may start right after either
        while i < n:
            c = text[i]
            if c <= ' ':
                i += 1
                continue
            if text[i:i + 2] == "//":
                i += 2
                while i < n and text[i] != '\n':
                    i += 1
                continue
            if text[i:i + 2] == "/*":
                i += 2
                end = text.find("*/", i)
                i = end + 2 if end != -1 else n
                continue
            break
        if i >= n:
            return

        if text[i] == '"':
            i += 1
            start = i
            end = text.find('"', i)
            if end == -1:
                yield text[start:n]
                i = n
            else:
                yield text[start:end]
                i = end + 1
            continue

        start = i
        while i < n and text[i] > ' ':
            i += 1
        yield text[start:i]
