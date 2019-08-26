# -*- coding: utf-8 -*-
#
# Copyright © Spyder Project Contributors
# Licensed under the terms of the MIT License
# (see spyder/__init__.py for details)

"""
Widget that handles communications between a console in debugging
mode and Spyder
"""

import re
import pdb

from IPython.core.history import HistoryManager
from qtpy.QtCore import Qt
from qtconsole.rich_jupyter_widget import RichJupyterWidget

from spyder.config.base import get_conf_path
from spyder.config.manager import CONF


class PdbHistory(HistoryManager):

    def _get_hist_file_name(self, profile=None):
        """
        Get default pdb history file name.

        The profile parameter is ignored, but must exist for compatibility with
        the parent class.
        """
        return get_conf_path('pdb_history.sqlite')


class DebuggingWidget(RichJupyterWidget):
    """
    Widget with the necessary attributes and methods to handle
    communications between a console in debugging mode and
    Spyder
    """
    PDB_HIST_MAX = 400

    def __init__(self, *args, **kwargs):
        super(DebuggingWidget, self).__init__(*args, **kwargs)
        # Adapted from qtconsole/frontend_widget.py
        # This adds 'ipdb> ' as a prompt self._highlighter recognises
        self._highlighter._ipy_prompt_re = re.compile(
            r'^(%s)?([ \t]*ipdb> |[ \t]*In \[\d+\]: |[ \t]*\ \ \ \.\.\.+: )'
            % re.escape(self.other_output_prefix))
        self._previous_prompt = None
        self._input_queue = []
        self._input_ready = False
        self._last_pdb_cmd = ''
        self._pdb_line_num = 0
        self.pdb_history_file = PdbHistory()
        self._control.history = [
            line[-1] for line in self.pdb_history_file.get_tail(
                self.PDB_HIST_MAX, include_latest=True)]

    def set_queued_input(self, client):
        """Change the kernel client input function queue calls."""
        old_input = client.input

        def queued_input(string):
            """If input is not ready, save it in a queue."""
            if not string.strip():
                # Must get the last genuine command
                string = self._last_pdb_cmd
            if self._input_ready:
                self._input_ready = False
                return old_input(string)
            elif self.is_debugging():
                self._input_queue.append(string)

        client.input = queued_input

    def _debugging_hook(self, debugging):
        """Catches debugging state."""
        # If debugging starts or stops, clear the input queue.
        self._input_queue = []
        self._pdb_line_num = 0

    # --- Public API --------------------------------------------------
    def write_to_stdin(self, line):
        """Send raw characters to the IPython kernel through stdin"""
        self._control.insert_text(line + '\n')
        self._reading = False
        self.kernel_client.input(line)

    def get_spyder_breakpoints(self):
        """Get spyder breakpoints."""
        return CONF.get('run', 'breakpoints', {})

    def set_spyder_breakpoints(self, force=False):
        """Set Spyder breakpoints into a debugging session"""
        self.call_kernel(interrupt=True).set_breakpoints(
            self.get_spyder_breakpoints())

    def dbg_exec_magic(self, magic, args=''):
        """Run an IPython magic while debugging."""
        if not self.is_debugging():
            return
        code = "!get_ipython().kernel.shell.run_line_magic('{}', '{}')".format(
                    magic, args)
        self.kernel_client.input(code)

    def refresh_from_pdb(self, pdb_state):
        """
        Refresh Variable Explorer and Editor from a Pdb session,
        after running any pdb command.

        See publish_pdb_state and notify_spyder in spyder_kernels
        """
        if 'step' in pdb_state and 'fname' in pdb_state['step']:
            fname = pdb_state['step']['fname']
            lineno = pdb_state['step']['lineno']
            self.sig_pdb_step.emit(fname, lineno)

        if 'namespace_view' in pdb_state:
            self.set_namespace_view(pdb_state['namespace_view'])

        if 'var_properties' in pdb_state:
            self.set_var_properties(pdb_state['var_properties'])

    # ---- Private API (overrode by us) ----------------------------
    def _handle_input_request(self, msg):
        """Save history and add a %plot magic."""
        if self._hidden:
            raise RuntimeError(
                'Request for raw input during hidden execution.')

        # Make sure that all output from the SUB channel has been processed
        # before entering readline mode.
        self.kernel_client.iopub_channel.flush()
        self._input_ready = True
        self._executing = False

        # While the widget thinks only one input is going on,
        # other functions can be sending messages to the kernel.
        # This must be properly processed to avoid dropping messages.
        # If the kernel was not ready, the messages are queued.
        if len(self._input_queue) > 0:
            msg = self._input_queue[0]
            del self._input_queue[0]
            self.kernel_client.input(msg)
            return

        def callback(line):
            if not self.is_debugging():
                return self.kernel_client.input(line)
            line = line.strip()

            # Save history to browse it later
            self._pdb_line_num += 1
            self.add_to_pdb_history(self._pdb_line_num, line)

            if line:
                self._last_pdb_cmd = line

            # must match ConsoleWidget.do_execute
            self._executing = True

            # This is the Spyder addition: add a %plot magic to display
            # plots while debugging
            if line.startswith('%plot '):
                line = line.split()[-1]
                code = "__spy_code__ = get_ipython().run_cell('%s')" % line
                self.kernel_client.input(code)
            else:
                self.kernel_client.input(line)
            self._highlighter.highlighting_on = False
        self._highlighter.highlighting_on = True

        prompt, password = msg['content']['prompt'], msg['content']['password']
        position = self._prompt_pos

        if (self._reading and
                (prompt, password, position) == self._previous_prompt):
            # This is a duplicate, don't reprint
            # This can happen when sending commands to pdb from the frontend.
            return

        self._previous_prompt = (prompt, password, position)
        # Reset reading in case it was interrupted
        self._reading = False
        self._readline(prompt=prompt, callback=callback,
                       password=password)

    def _show_prompt(self, prompt=None, html=False, newline=True):
        """
        Writes a new prompt at the end of the buffer.
        """
        if prompt in ['(Pdb) ', 'ipdb> ']:
            html = True
            prompt = '<span class="in-prompt">%s</span>' % prompt
        super(DebuggingWidget, self)._show_prompt(prompt, html, newline)

    def _event_filter_console_keypress(self, event):
        """Handle Key_Up/Key_Down while debugging."""
        key = event.key()
        if self._reading:
            self._control.current_prompt_pos = self._prompt_pos
            if key == Qt.Key_Up:
                self._control.browse_history(backward=True)
                return True
            elif key == Qt.Key_Down:
                self._control.browse_history(backward=False)
                return True
            elif key in (Qt.Key_Return, Qt.Key_Enter):
                self._control.reset_search_pos()
            else:
                self._control.hist_wholeline = False
            return super(DebuggingWidget,
                         self)._event_filter_console_keypress(event)
        else:
            return super(DebuggingWidget,
                         self)._event_filter_console_keypress(event)

    def is_debugging(self):
        """Check if we are debugging."""
        return self.spyder_kernel_comm._debugging

    def add_to_pdb_history(self, line_num, line):
        """Add command to history"""
        self._control.histidx = None
        if not line:
            return
        line = line.strip()

        # If repeated line
        if len(self._control.history) > 0 and self._control.history[-1] == line:
            return

        cmd = line.split(" ")[0]
        args = line.split(" ")[1:]
        is_pdb_cmd = "do_" + cmd in dir(pdb.Pdb)
        if cmd and (not is_pdb_cmd or len(args) > 0):
            self._control.history.append(line)
            self.pdb_history_file.store_inputs(line_num, line)