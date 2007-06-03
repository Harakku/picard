# -*- coding: utf-8 -*-
#
# Picard, the next-generation MusicBrainz tagger
# Copyright (C) 2004 Robert Kaye
# Copyright (C) 2006 Lukáš Lalinský
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

import glob
import os.path
import shutil
import sys
import re
import traceback
from PyQt4 import QtCore
from picard.metadata import Metadata
from picard.ui.item import Item
from picard.script import ScriptParser
from picard.similarity import similarity2
from picard.util.thread import proxy_to_main
from picard.util import (
    decode_filename,
    encode_filename,
    make_short_filename,
    replace_win32_incompat,
    replace_non_ascii,
    sanitize_filename,
    partial,
    unaccent,
    format_time,
    LockableObject,
    )

class File(LockableObject, Item):

    __id_counter = 0
    @staticmethod
    def new_id():
        File.__id_counter += 1
        return File.__id_counter

    PENDING = 0
    NORMAL = 1
    CHANGED = 2
    ERROR = 3
    REMOVED = 4

    def __init__(self, filename):
        super(File, self).__init__()
        self.id = self.new_id()
        self.filename = filename
        self.base_filename = os.path.basename(filename)
        self.state = File.PENDING
        self.error = None

        self.orig_metadata = Metadata()
        self.user_metadata = Metadata()
        self.server_metadata = Metadata()
        self.saved_metadata = self.server_metadata
        self.metadata = self.user_metadata

        self.orig_metadata['title'] = os.path.basename(self.filename)
        self.orig_metadata['~#length'] = 0
        self.orig_metadata['~length'] = format_time(0)

        self.user_metadata.copy(self.orig_metadata)
        self.server_metadata.copy(self.orig_metadata)

        self.similarity = 1.0
        self.parent = None

    def __repr__(self):
        return '<File #%d %r>' % (self.id, self.base_filename)

    def load(self, finished=None):
        """Load metadata from the file."""
        del self.metadata['title']
        self.tagger.load_thread.add_task(self._load_thread, finished)

    def _load_thread(self, finished):
        if self.state != File.PENDING:
            return
        self.log.debug("Loading file %r", self)
        error = None
        try:
            self._load()
        except Exception, e:
            self.log.error(traceback.format_exc())
            error = str(e)
        proxy_to_main(self._load_thread_finished, finished, error)

    def has_error(self):
        return self.state == File.ERROR

    def _load_thread_finished(self, finished, error):
        if self.state != File.PENDING:
            return
        self.error = error
        self.state = (self.error is None) and File.NORMAL or File.ERROR
        self._post_load()
        self.update()
        if finished:
            finished(self)

    def _post_load(self):
        filename, extension = os.path.splitext(os.path.basename(self.filename))
        self.metadata['~extension'] = extension[1:].lower()
        self.metadata['~length'] = format_time(self.metadata['~#length'])
        if 'title' not in self.metadata:
            self.metadata['title'] = filename
        if 'tracknumber' not in self.metadata:
            match = re.match("(?:track)?\s*(?:no|nr)?\s*(\d+)", filename, re.I)
            if match:
                try:
                    tracknumber = int(match.group(1))
                except ValueError:
                    pass
                else:
                    self.metadata['tracknumber'] = str(tracknumber)
        self.orig_metadata.copy(self.metadata)

    def _load(self):
        """Load metadata from the file."""
        raise NotImplementedError

    def save(self):
        self.metadata.strip_whitespace()
        self._save()

    def _save(self):
        """Save the metadata."""
        raise NotImplementedError

    def __script_to_filename(self, format, settings):
        metadata = Metadata()
        metadata.copy(self.metadata)
        # replace incompatible characters
        for name in metadata.keys():
            value = metadata[name]
            if isinstance(value, basestring):
                value = sanitize_filename(value)
                if settings["windows_compatible_filenames"] or sys.platform == "win32":
                    value = replace_win32_incompat(value)
                if settings["ascii_filenames"]:
                    if isinstance(value, unicode):
                        value = unaccent(value)
                    value = replace_non_ascii(value)
                metadata[name] = value
        return ScriptParser().eval(format, metadata)

    def make_filename(self, settings=None):
        """Constructs file name based on metadata and file naming formats."""
        if settings is None:
            settings = self.config.setting

        filename = self.filename
        if settings["move_files"]:
            new_dirname = settings["move_files_to"]
            if not os.path.isabs(new_dirname):
                new_dirname = os.path.normpath(os.path.join(os.path.dirname(filename), new_dirname))
        else:
            new_dirname = os.path.dirname(filename)
        old_dirname = new_dirname
        new_filename, ext = os.path.splitext(os.path.basename(filename))

        if settings["rename_files"]:
            # expand the naming format
            if self.metadata['compilation'] == '1':
                format = settings['va_file_naming_format']
            else:
                format = settings['file_naming_format']
            new_filename = self.__script_to_filename(format, settings)
            if not settings['move_files']:
                new_filename = os.path.basename(new_filename)
            new_filename = make_short_filename(new_dirname, new_filename)
            # win32 compatibility fixes
            if settings['windows_compatible_filenames'] or sys.platform == 'win32':
                new_filename = new_filename.replace('./', '_/').replace('.\\', '_\\')

        return os.path.join(new_dirname, new_filename + ext.lower())

    def save_images(self):
        """Save the cover images to disk."""
        if not self.metadata.images:
            return
        settings = self.config.setting
        filename = self.__script_to_filename(self.config.setting["cover_image_filename"], settings)
        if not filename:
            filename = "cover"
        filename = os.path.join(os.path.dirname(self.filename), filename)
        if settings['windows_compatible_filenames'] or sys.platform == 'win32':
            filename = filename.replace('./', '_/').replace('.\\', '_\\')
        filename = encode_filename(filename)
        i = 0
        for mime, data in self.metadata.images:
            image_filename = filename
            ext = ".jpg" # TODO
            if i > 0:
                image_filename = "%s (%d)" % (filename, i)
            i += 1
            while os.path.exists(image_filename + ext):
                if os.path.getsize(image_filename + ext) == len(data):
                    self.log.debug("Identical file size, not saving %r", image_filename)
                    break
                image_filename = "%s (%d)" % (filename, i)
                i += 1
            else:
                self.log.debug("Saving cover images to %r", image_filename)
                f = open(image_filename + ext, "wb")
                f.write(data)
                f.close()

    def move_additional_files(self, old_filename):
        old_path = encode_filename(os.path.dirname(old_filename))
        new_path = encode_filename(os.path.dirname(self.filename))
        patterns = encode_filename(self.config.setting["move_additional_files_pattern"])
        patterns = filter(bool, [p.strip() for p in patterns.split()])
        files = []
        for pattern in patterns:
            # FIXME glob1 is not documented, maybe we need our own implemention?
            for old_file in glob.glob1(old_path, pattern):
                new_file = os.path.join(new_path, old_file)
                old_file = os.path.join(old_path, old_file)
                if self.tagger.get_file_by_filename(decode_filename(old_file)):
                    self.log.debug("File loaded in the tagger, not moving %r", old_file)
                    continue
                self.log.debug("Moving %r to %r", old_file, new_file)
                shutil.move(old_file, new_file)

    def remove(self):
        if self.parent:
            self.log.debug("Removing %r from %r", self, self.parent)
            self.parent.remove_file(self)
            self.tagger.puidmanager.update(self.metadata['musicip_puid'], self.metadata['musicbrainz_trackid'])
        self.state = File.REMOVED

    def move(self, parent):
        if parent != self.parent:
            self.log.debug("Moving %r from %r to %r", self, self.parent, parent)
            if self.parent:
                self.clear_pending()
                self.parent.remove_file(self)
            self.parent = parent
            self.parent.add_file(self)
            self.tagger.puidmanager.update(self.metadata['musicip_puid'], self.metadata['musicbrainz_trackid'])

    def _move(self, parent):
        if parent != self.parent:
            self.log.debug("Moving %r from %r to %r", self, self.parent, parent)
            if self.parent:
                self.parent.remove_file(self)
            self.parent = parent
            self.tagger.puidmanager.update(self.metadata['musicip_puid'], self.metadata['musicbrainz_trackid'])

    def supports_tag(self, name):
        """Returns whether tag ``name`` can be saved to the file."""
        return True

    def is_saved(self):
        return self.similarity == 1.0 and self.state == File.NORMAL

    def update(self, signal=True):
        for name, values in self.metadata.rawitems():
            if not name.startswith('~') and self.supports_tag(name):
                if self.orig_metadata.getall(name) != values:
                    #print name, values, self.orig_metadata.getall(name)
                    self.similarity = self.orig_metadata.compare(self.metadata)
                    if self.state in (File.CHANGED, File.NORMAL):
                        self.state = File.CHANGED
                    break
        else:
            self.similarity = 1.0
            if self.state in (File.CHANGED, File.NORMAL):
                self.state = File.NORMAL
        if signal:
            self.log.debug("Updating file %r", self)
            self.parent.update_file(self)

    def can_save(self):
        """Return if this object can be saved."""
        return True

    def can_remove(self):
        """Return if this object can be removed."""
        return True

    def can_edit_tags(self):
        """Return if this object supports tag editing."""
        return True

    def can_analyze(self):
        """Return if this object can be fingerprinted."""
        return True

    def can_refresh(self):
        return False

    def _info(self, file):
        self.metadata["~#length"] = int(file.info.length * 1000)
        if hasattr(file.info, 'bitrate') and file.info.bitrate:
            self.metadata['~#bitrate'] = file.info.bitrate / 1000.0
        if hasattr(file.info, 'sample_rate') and file.info.sample_rate:
            self.metadata['~#sample_rate'] = file.info.sample_rate
        if hasattr(file.info, 'channels') and file.info.channels:
            self.metadata['~#channels'] = file.info.channels
        if hasattr(file.info, 'bits_per_sample') and file.info.bits_per_sample:
            self.metadata['~#bits_per_sample'] = file.info.bits_per_sample
        self.metadata['~format'] = self.__class__.__name__.replace('File', '')

    def get_state(self):
        return self._state

    def set_state(self, state, update=False):
        self._state = state
        if update:
            self.update()
        self.tagger.emit(QtCore.SIGNAL("file_state_changed"))

    state = property(get_state, set_state)

    def column(self, column):
        return self.metadata[column], self.similarity

    def _compare_to_track(self, track):
        """
        Compare file metadata to a MusicBrainz track.

        Weigths:
          * title                = 13
          * artist name          = 3
          * release name         = 5
          * length               = 10
          * number of tracks     = 3

        """
        total = 0.0
        parts = []

        if 'title' in self.metadata:
            a = self.metadata['title']
            b = track.title[0].text
            parts.append((similarity2(a, b), 13))
            total += 13

        if 'artist' in self.metadata:
            a = self.metadata['artist']
            b = track.artist[0].name[0].text
            parts.append((similarity2(a, b), 4))
            total += 4

        if 'album' in self.metadata:
            a = self.metadata['album']
            b = track.release_list[0].release[0].title[0].text
            parts.append((similarity2(a, b), 5))
            total += 5

        a = self.metadata['~#length']
        if a > 0 and 'duration' in track.children:
            b = int(track.duration[0].text)
            score = 1.0 - min(abs(a - b), 30000) / 30000.0
            parts.append((score, 10))
            total += 10

        track_list = track.release_list[0].release[0].track_list[0]
        if 'totaltracks' in self.metadata and 'count' in track_list.attribs:
            try:
                a = int(self.metadata['totaltracks'])
                b = int(track_list.count)
                if a > b:
                    score = 0.0
                elif a < b:
                    score = 0.3
                else:
                    score = 1.0
                parts.append((score, 4))
                total += 4
            except ValueError:
                pass

        return reduce(lambda x, y: x + y[0] * y[1] / total, parts, 0.0)

    def _lookup_finished(self, lookuptype, document, http, error):
        try:
            tracks = document.metadata[0].track_list[0].track
        except (AttributeError, IndexError):
            tracks = None

        # no matches
        if not tracks:
            self.tagger.window.set_statusbar_message(N_("No matching tracks for file %s"), self.filename, timeout=3000)
            self.clear_pending()
            return

        # multiple matches -- calculate similarities to each of them
        matches = []
        for track in tracks:
            matches.append((self._compare_to_track(track), track))
        matches.sort(reverse=True)
        self.log.debug("Track matches: %r", matches)

        if lookuptype == 'puid':
            threshold = self.config.setting['puid_lookup_threshold']
        else:
            threshold = self.config.setting['file_lookup_threshold']

        if matches[0][0] < threshold:
            self.tagger.window.set_statusbar_message(N_("No matching tracks for file %s"), self.filename, timeout=3000)
            self.clear_pending()
            return
        self.tagger.window.set_statusbar_message(N_("File %s identified!"), self.filename, timeout=3000)
        self.clear_pending()

        albumid = matches[0][1].release_list[0].release[0].id
        trackid = matches[0][1].id
        if lookuptype == 'puid':
            self.tagger.puidmanager.add(self.metadata['musicip_puid'], trackid)
        self.tagger.move_file_to_track(self, albumid, trackid)

    def lookup_puid(self, puid):
        """ Try to identify the file using the PUID. """
        self.tagger.window.set_statusbar_message(N_("Looking up the PUID for file %s..."), self.filename)
        self.tagger.xmlws.find_tracks(partial(self._lookup_finished, 'puid'), puid=puid)

    def lookup_metadata(self):
        """ Try to identify the file using the existing metadata. """
        self.tagger.window.set_statusbar_message(N_("Looking up the metadata for file %s..."), self.filename)
        self.tagger.xmlws.find_tracks(partial(self._lookup_finished, 'metadata'),
            track=self.metadata.get('title', ''),
            artist=self.metadata.get('artist', ''),
            release=self.metadata.get('album', ''),
            tnum=self.metadata.get('tracknumber', ''),
            tracks=self.metadata.get('totaltracks', ''),
            qdur=str(self.metadata.get('~#length', 0) / 2000),
            limit=7)

    def set_pending(self):
        if self.state == File.REMOVED:
            return
        self.state = File.PENDING
        self.update()

    def clear_pending(self):
        if self.state == File.PENDING:
            self.state = File.NORMAL
            self.update()

    def _get_tracknumber(self):
        try:
            return int(self.metadata["tracknumber"])
        except:
            return 0
    tracknumber = property(_get_tracknumber, doc="The track number as an int.")

    def _get_discnumber(self):
        try:
            return int(self.metadata["discnumber"])
        except:
            return 0
    discnumber = property(_get_discnumber, doc="The disc number as an int.")
