#!/usr/bin/env python3

# Copyright (C) 2016 - 2017 Sylvia van Os <sylvia@hackerchick.me>
#
# Pext pass module is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import gettext
import html
import re
import os
from datetime import datetime
from os.path import expanduser
from subprocess import check_output
from shlex import quote

import pexpect

from pext_base import ModuleBase
from pext_helpers import Action, SelectionType

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class Module(ModuleBase):
    def init(self, settings, q):
        try:
            lang = gettext.translation('pext_module_pass', localedir=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'locale'), languages=[settings['_locale']])
        except FileNotFoundError:
            lang = gettext.NullTranslations()
            print("No {} translation available for pext_module_pass".format(settings['_locale']))

        lang.install()

        self.binary = "pass" if ('binary' not in settings) else settings['binary']
        self.data_location = expanduser("~/.password-store/") if ('directory' not in settings) else expanduser(settings['directory'])
        os.environ['PASSWORD_STORE_DIR'] = self.data_location

        try:
            os.mkdir(self.data_location)
        except OSError:
            # Probably already exists, that's okay
            pass

        self.q = q
        self.settings = settings

        self.ANSIEscapeRegex = re.compile('(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]')
        self.passwordEntries = {}

        self._get_commands()
        self._get_entries()

        self._init_watchdog(q)

    def _init_watchdog(self, q):
        # Initialize the EventHandler and make it watch the password store
        event_handler = EventHandler(q, self)
        self.observer = Observer()
        self.observer.schedule(event_handler, self._get_data_location(), recursive=True)
        self.observer.start()

    def _get_data_location(self):
        return self.data_location

    def _get_unsupported_commands(self):
        return ["[ls]", "find", "[show]", "grep"]

    def _get_commands(self):
        try:
            commandText = check_output([self.binary, "--help"])
        except FileNotFoundError:
            self.q.put([Action.critical_error, _("pass is not installed. Please see https://www.passwordstore.org/")])
            return

        command = None
        command_description = ""
        for line in commandText.splitlines():
            strippedLine = line.lstrip().decode("utf-8")
            if strippedLine[:4] == "pass" or not strippedLine:
                if command_description and self.settings['_api_version'] >= [0, 3, 1]:
                    self.q.put([Action.set_command_info, command, "<b>{}</b><br/><br/>{}".format(html.escape(command), command_description)])
                    command_description = ""

                if strippedLine[:4] == "pass":
                    command = strippedLine[5:]
                    if not command.split(" ", 1)[0] in self._get_unsupported_commands():
                        self.q.put([Action.add_command, command])
                    else:
                        command = None

            elif command:
                command_description += "{} ".format(html.escape(strippedLine))


    def _get_entries(self):
        passDir = self._get_data_location()

        unsortedPasswords = []
        for root, dirs, files in os.walk(passDir):
            for name in files:
                if name[-4:] == ".gpg":
                    unsortedPasswords.append(os.path.join(root, name))

        for password in sorted(unsortedPasswords, key=lambda name: os.path.getatime(os.path.join(root, name)), reverse=True):
            entry = password[len(passDir):-4]
            self.q.put([Action.add_entry, entry])
            if self.settings['_api_version'] >= [0, 3, 1]:
                self.q.put([Action.set_entry_info, entry, _("<b>{}</b><br/><br/><b>Last opened</b><br/>{}<br/><br/><b>Last modified</b><br/>{}").format(html.escape(entry), datetime.fromtimestamp(os.path.getatime(password)).replace(microsecond=0), datetime.fromtimestamp(os.path.getmtime(password)).replace(microsecond=0))])
            if self.settings['_api_version'] >= [0, 4, 0]:
                self.q.put([Action.set_entry_context, entry, [_("Open"), _("Edit"), _("Remove")]])


    def _run_command(self, command, printOnSuccess=False, hideErrors=False, prefillInput=''):
        # If we edit a password, make sure to get the original input first so we can show the user
        if command[0] == "edit" and len(command) == 2:
            prefillData = self._run_command(["show", command[1]], hideErrors=True)

            if self.proc['result'] == pexpect.TIMEOUT:
                return None

            if prefillData is None:
                prefillData = ''

            return self._run_command(["insert", "-fm", command[1]], printOnSuccess=True, prefillInput=prefillData.rstrip())

        sanitizedCommandList = [quote(commandPart) for commandPart in command]
        command = " ".join(sanitizedCommandList)

        proc = pexpect.spawn('/bin/sh', ['-c', self.binary + " " + command + (" 2>/dev/null" if hideErrors else "")])
        return self._process_proc_output(proc, command, printOnSuccess, hideErrors, prefillInput)

    def _process_proc_output(self, proc, command, printOnSuccess=False, hideErrors=False, prefillInput=''):
        possibleResults = [pexpect.EOF, pexpect.TIMEOUT, "[Y/n]", "[y/N]", "Enter password ", "Retype password ", " and press Ctrl+D when finished:"]
        result = proc.expect_exact(possibleResults, timeout=30 if self.settings['_api_version'] >= [0, 7, 0] else 3)
        if result == 0:
            self.proc = {'result': possibleResults[result]}
        elif result == 1:
            self.proc = {'result': possibleResults[result]}
            self.q.put([Action.add_error, _("Timeout error while running '{}'").format(command)])
            if proc.before:
                self.q.put([Action.add_error, _("Command output: {}").format(self.ANSIEscapeRegex.sub('', proc.before.decode("utf-8")))])

            return None
        elif result == 2 or result == 3:
            proc.setecho(False)
            question = proc.before.decode("utf-8")

            if (result == 2):
                self.proc = {'proc': proc,
                             'command': command,
                             'type': Action.ask_question_default_yes,
                             'printOnSuccess': printOnSuccess,
                             'hideErrors': hideErrors,
                             'prefillInput': prefillInput,
                             'result': possibleResults[result]}
                self.q.put([Action.ask_question_default_yes, question])
            else:
                self.proc = {'proc': proc,
                             'command': command,
                             'type': Action.ask_question_default_no,
                             'printOnSuccess': printOnSuccess,
                             'hideErrors': hideErrors,
                             'prefillInput': prefillInput,
                             'result': possibleResults[result]}
                self.q.put([Action.ask_question_default_no, question])

            return None
        elif result == 4 or result == 5:
            printOnSuccess = False
            proc.setecho(False)
            self.proc = {'proc': proc,
                         'command': command,
                         'type': Action.ask_input_password,
                         'printOnSuccess': printOnSuccess,
                         'hideErrors': hideErrors,
                         'prefillInput': prefillInput,
                         'result': possibleResults[result]}
            self.q.put([Action.ask_input_password, proc.after.decode("utf-8")])

            return None
        elif result == 6:
            self.proc = {'proc': proc,
                         'command': command,
                         'type': Action.ask_input_multi_line,
                         'printOnSuccess': printOnSuccess,
                         'hideErrors': hideErrors,
                         'prefillInput': prefillInput,
                         'result': possibleResults[result]}
            self.q.put([Action.ask_input_multi_line, proc.before.decode("utf-8").lstrip(), prefillInput])

            proc.setecho(False)

            return None

        proc.close()
        exitCode = proc.exitstatus

        message = self.ANSIEscapeRegex.sub('', proc.before.decode("utf-8")) if proc.before else ""

        self.q.put([Action.set_filter, ""])

        if exitCode == 0:
            if printOnSuccess and message:
                self.q.put([Action.add_message, message])

            return message
        else:
            self.q.put([Action.add_error, message if message else _("Error code {} running '{}'. More info may be logged to the console").format(str(exitCode), command)])

            return None

    def process_response(self, response):
        if self.proc['type'] == Action.ask_question_default_yes or self.proc['type'] == Action.ask_question_default_no:
            self.proc['proc'].waitnoecho()
            self.proc['proc'].sendline('y' if response else 'n')
            self.proc['proc'].setecho(True)
        elif self.proc['type'] == Action.ask_input or self.proc['type'] == Action.ask_input_password:
            self.proc['proc'].waitnoecho()
            if response is None:
                self.proc['proc'].close()
            else:
                self.proc['proc'].sendline(response)
                self.proc['proc'].setecho(True)
        elif self.proc['type'] == Action.ask_input_multi_line:
            self.proc['proc'].waitnoecho()
            if response is None:
                # At this point, pass won't let us exit out safely, so we
                # write the prefilled data
                for line in self.proc['prefillInput'].splitlines():
                    self.proc['proc'].sendline(line)
            else:
                for line in response.splitlines():
                    self.proc['proc'].sendline(line)

            self.proc['proc'].sendcontrol("d")
            self.proc['proc'].setecho(True)

        self._process_proc_output(self.proc['proc'], self.proc['command'], printOnSuccess=self.proc['printOnSuccess'], hideErrors=self.proc['hideErrors'], prefillInput=self.proc['prefillInput'])

    def stop(self):
        self.observer.stop()

    def selection_made(self, selection):
        if len(selection) == 0:
            # We're at the main menu
            self.passwordEntries = {}
            self.q.put([Action.set_header])
            self.q.put([Action.replace_command_list, []])
            self.q.put([Action.replace_entry_list, []])
            self._get_commands()
            self._get_entries()
        elif len(selection) == 1:
            if selection[0]["type"] == SelectionType.command:
                parts = selection[0]["value"].split(" ")
                self._run_command(parts)
                self.q.put([Action.set_selection, []])
            elif selection[0]["type"] == SelectionType.entry:
                if self.settings['_api_version'] >= [0, 4, 0]:
                    if selection[0]["context_option"] == _("Edit"):
                        self._run_command(["edit", selection[0]["value"]], hideErrors=True)
                        self.q.put([Action.set_selection, []])
                        return
                    elif selection[0]["context_option"] == _("Remove"):
                        self._run_command(["rm", selection[0]["value"]], hideErrors=True)
                        self.q.put([Action.set_selection, []])
                        return

                results = self._run_command(["show", selection[0]["value"]], hideErrors=True)
                if results is None:
                    self.q.put([Action.set_selection, []])
                    return

                self.q.put([Action.set_header, selection[0]["value"]])
                self.q.put([Action.replace_entry_list, []])
                self.q.put([Action.replace_command_list, []])

                for line in results.rstrip().splitlines():
                    if len(self.passwordEntries) == 0:
                        self.passwordEntries["********"] = line
                        self.q.put([Action.add_entry, "********"])
                    else:
                        self.passwordEntries[line] = line
                        self.q.put([Action.add_entry, line])
            else:
                self.q.put([Action.critical_error, _("Unexpected selection_made value: {}").format(selection)])
        elif len(selection) == 2:
            # We're selecting a password
            if selection[1]["value"] == "********":
                self.q.put([Action.copy_to_clipboard, self.passwordEntries["********"]])
            else:
                # Get the final part to prepare for copying. For example, if
                # the entry is named URL: https://example.org/", only copy
                # "https://example.org/" to the clipboard
                copyStringParts = self.passwordEntries[selection[1]["value"]].split(": ", 1)

                copyString = copyStringParts[1] if len(copyStringParts) > 1 else copyStringParts[0]
                self.q.put([Action.copy_to_clipboard, copyString])

            self.q.put([Action.close])
        else:
            self.q.put([Action.critical_error, _("Unexpected selection_made value: {}").format(selection)])

class EventHandler(FileSystemEventHandler):
    def __init__(self, q, store):
        self.q = q
        self.store = store

    def on_deleted(self, event):
        if event.is_directory or len(self.store.passwordEntries) > 0:
            return

        entry_name = event.src_path[len(self.store._get_data_location()):]

        if entry_name[-4:] != ".gpg":
            return

        self.q.put([Action.remove_entry, entry_name[:-4]])

    def on_modified(self, event):
        if event.is_directory or len(self.store.passwordEntries) > 0:
            return

        entry_name = event.src_path[len(self.store._get_data_location()):]

        if entry_name[-4:] != ".gpg":
            return

        self.q.put([Action.prepend_entry, entry_name[:-4]])
        # As this event also gets called when a file gets created, it may
        # generate warnings in Pext to call this. These warnings are harmless
        self.q.put([Action.remove_entry, entry_name[:-4]])
