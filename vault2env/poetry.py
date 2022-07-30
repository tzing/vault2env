import logging
import typing

from cleo.events.console_events import COMMAND
from cleo.formatters.style import Style
from cleo.io.outputs.output import Verbosity
from poetry.console.commands.run import RunCommand
from poetry.console.commands.shell import ShellCommand
from poetry.plugins.application_plugin import ApplicationPlugin

import vault2env.config

if typing.TYPE_CHECKING:
    from cleo.events.console_command_event import ConsoleCommandEvent
    from cleo.events.event_dispatcher import EventDispatcher
    from cleo.io.outputs.output import Output
    from poetry.console.application import Application

logger = logging.getLogger(__name__)


class Vault2EnvPlugin(ApplicationPlugin):
    def activate(self, application: "Application") -> None:
        application.event_dispatcher.add_listener(COMMAND, self.load_secret)

    def load_secret(
        self,
        event: "ConsoleCommandEvent",
        event_name: str,
        dispatcher: "EventDispatcher",
    ) -> None:
        if not isinstance(event.command, (RunCommand, ShellCommand)):
            return

        self.setup_output(event.io.output)
        logger.debug("Start vault2env poetry plugin")

        config = vault2env.config.load_config()
        if not config:
            # already log the errors in load_config
            return {}

    def setup_output(self, output: "Output") -> None:
        """Forwards internal messages to cleo.

        Vault2env internally uses logging module for showing messages to users.
        But cleo hides the logs, unless `-vv` (VERY_VERBOSE) is set, this made
        it harder to show warnings or errors.

        So it forwards all internal logs from vault2env to cleo. (Re)assign the
        verbosity level in the Handler and colored the output using the custom
        Formatter, powered with cleo's formatter."""
        # set output format
        output.formatter.set_style("debug", Style("white"))
        output.formatter.set_style("warning", Style("yellow", options=["bold"]))

        # send internal message to cleo
        # see docstring in Handler for details
        handler = Handler(output)
        handler.setFormatter(Formatter())

        root_logger = logging.getLogger("vault2env")
        root_logger.setLevel(logging.NOTSET)
        root_logger.propagate = False
        root_logger.addHandler(handler)


class Handler(logging.Handler):
    """Send the logs to cleo's IO module."""

    VERBOSITY = {
        logging.DEBUG: Verbosity.DEBUG,
        logging.INFO: Verbosity.VERBOSE,
        logging.WARNING: Verbosity.NORMAL,
        logging.ERROR: Verbosity.QUIET,
        logging.CRITICAL: Verbosity.QUIET,
    }

    def __init__(self, output: "Output") -> None:
        super().__init__(logging.NOTSET)
        self.output = output

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return

        verbosity = self.VERBOSITY.get(record.levelno, Verbosity.NORMAL)
        self.output.write_line(msg, verbosity=verbosity)


class Formatter(logging.Formatter):
    """Translates internal expression into cleo's format."""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)

        # tag translate
        # uses builtin tags for aligning the appearance with poetry and other plugins
        msg = msg.replace("<em>", "<info>").replace("</em>", "</info>")
        msg = msg.replace("<data>", "<comment>").replace("</data>", "</comment>")

        # add color
        if record.levelno >= logging.ERROR:
            msg = f"<error>{msg}</error>"
        elif record.levelno >= logging.WARNING:
            msg = f"<warning>{msg}</warning>"
        elif record.levelno <= logging.DEBUG:
            msg = f"<debug>{msg}</debug>"

        return msg