# coding=utf-8
from __future__ import absolute_import, unicode_literals, print_function, division

__author__ = "Gina Häußge <osd@foosel.net>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'
__copyright__ = "Copyright (C) 2015 The OctoPrint Project - Released under terms of the AGPLv3 License"

from octoprint.comm.protocol import Protocol, ThreeDPrinterProtocolMixin, FileStreamingProtocolMixin, \
	MotorControlProtocolMixin, FanControlProtocolMixin, ProtocolState
from octoprint.comm.transport import Transport, LineAwareTransportWrapper, \
	PushingTransportWrapper, PushingTransportWrapperListener, TransportListener

from octoprint.comm.protocol.reprap.commands import Command, to_command
from octoprint.comm.protocol.reprap.commands.atcommand import AtCommand
from octoprint.comm.protocol.reprap.commands.gcode import GcodeCommand

from octoprint.comm.protocol.reprap.flavors import GenericFlavor, all_flavors

from octoprint.comm.protocol.reprap.util import normalize_command_handler_result, SendQueue, LineHistory

from octoprint.util import TypedQueue, TypeAlreadyInQueue

from octoprint.job import LocalGcodeFilePrintjob, SDFilePrintjob, \
	LocalGcodeStreamjob

from octoprint.util import to_str, to_unicode, protectedkeydict, CountedEvent, monotonic_time

from octoprint.events import Events

try:
	import queue
except ImportError:
	import Queue as queue

import collections
import logging
import threading
import time


GCODE_TO_EVENT = {
	# pause for user input
	"M226": Events.WAITING,
	"M0": Events.WAITING,
	"M1": Events.WAITING,
	# dwell command
	"G4": Events.DWELL,

	# part cooler
	"M245": Events.COOLING,

	# part conveyor
	"M240": Events.CONVEYOR,

	# part ejector
	"M40": Events.EJECT,

	# user alert
	"M300": Events.ALERT,

	# home print head
	"G28": Events.HOME,

	# emergency stop
	"M112": Events.E_STOP,

	# motors on/off
	"M80": Events.POWER_ON,
	"M81": Events.POWER_OFF,
}

CAPABILITY_AUTOREPORT_TEMP = "AUTOREPORT_TEMP"
CAPABILITY_AUTOREPORT_SD_STATUS = "AUTOREPORT_SD_STATUS"
CAPABILITY_BUSY_PROTOCOL = "BUSY_PROTOCOL"

class ReprapGcodeProtocol(Protocol, ThreeDPrinterProtocolMixin, MotorControlProtocolMixin,
                          FanControlProtocolMixin, FileStreamingProtocolMixin,
                          PushingTransportWrapperListener, TransportListener):

	supported_jobs = [LocalGcodeFilePrintjob,
	                  LocalGcodeStreamjob,
	                  SDFilePrintjob]

	@staticmethod
	def get_flavor_attributes_starting_with(flavor, prefix):
		return [x for x in dir(flavor) if x.startswith(prefix)]

	@classmethod
	def get_attributes_starting_with(cls, prefix):
		return dict((x, getattr(cls, x)) for x in dir(cls) if x.startswith(prefix))

	def __init__(self, flavor=None,
	             connection_timeout=30.0,
	             communication_timeout=10.0,
	             temperature_interval_idle=5.0,
	             temperature_interval_printing=5.0,
	             temperature_interval_autoreport=2.0,
	             sd_status_interval_autoreport=1.0):
		super(ReprapGcodeProtocol, self).__init__()

		self._phase_logger = logging.getLogger(__name__ + ".command_phases")

		self.flavor = flavor if flavor is not None else GenericFlavor
		self.timeouts = dict(
			connection=connection_timeout,
			communication=communication_timeout
		)
		self.interval = dict(
			temperature_idle=temperature_interval_idle,
			temperature_printing=temperature_interval_printing,
			temperature_autoreport=temperature_interval_autoreport,
			sd_status_autoreport=sd_status_interval_autoreport
		)

		flavor_comm_attrs = self.get_flavor_attributes_starting_with(self.flavor, "comm_")
		flavor_message_attrs = self.get_flavor_attributes_starting_with(self.flavor, "message_")
		flavor_error_attrs = self.get_flavor_attributes_starting_with(self.flavor, "error_")

		self._registered_messages = flavor_comm_attrs + flavor_message_attrs
		self._error_messages = flavor_error_attrs

		self._handlers_gcode = self.get_attributes_starting_with("_gcode_")
		self._handlers_atcommand = self.get_attributes_starting_with("_atcommand_")
		self._handlers_command_phase = self.get_attributes_starting_with("_command_phase_")

		self._capability_support = dict()
		self._capability_support[CAPABILITY_AUTOREPORT_TEMP] = True

		self._transport = None

		self._internal_state = dict(
			# temperature and heating related
			temperatures=dict(),
			heating_start=None,
			heating_lost=0,
			heating=False,
			temperature_autoreporting=False,

			# current stuff
			current_tool=0,
			former_tool=0,
			current_z=None,

			# sd status
			sd_available=False,
			sd_files=[],
			sd_files_temp=None,
			sd_status_autoreporting=False,

			# resend status
			resend_requested=None,
			resend_linenumber=None,
			resend_count=None,

			# protocol status
			long_running_command=False,

			# misc
			only_from_job=False,
			trigger_events=True,
			ignore_ok=0,

			# timeout
			timeout = None,
			timeout_consecutive=0,

			# firmware info
			firmware_identified=False,
			firmware_name=None,
			firmware_info=dict(),
			firmware_capabilities=dict()
		)
		self._protected_state = protectedkeydict(self._internal_state)

		self._command_queue = TypedQueue()
		self._clear_to_send = CountedEvent(max=10, name="comm.clear_to_send")
		self._send_queue = SendQueue()

		self._current_linenumber = 1
		self._last_lines = LineHistory(maxlen = 50)
		self._last_communication_error = None

		self._gcode_hooks = dict(queuing=dict(),
		                         queued=dict(),
		                         sending=dict(),
		                         sent=dict())

		self._atcommand_hooks = dict(queuing=dict(),
		                             sending=dict())

		# temperature polling
		self._temperature_poller = None

		# terminal logging
		self._terminal_log = collections.deque([], 20)

		# resend logging
		self._log_resends = True
		self._log_resends_rate_start = None
		self._log_resends_rate_count = 0
		self._log_resends_rate_frame = 60
		self._log_resends_max = 5

		# mutexes
		self._line_mutex = threading.RLock()
		self._send_queue_mutex = threading.RLock()

		# sending thread
		self._send_queue_active = False
		self._sending_thread = None

	@property
	def _active(self):
		return self._transport is not None and self._transport.active and self._send_queue_active

	def connect(self, transport, transport_args=None, transport_kwargs=None):
		if not isinstance(transport, Transport):
			raise ValueError("transport must be a Transport subclass but is a {} instead".format(type(transport)))

		self._internal_state["timeout"] = self._get_timeout("connection")

		transport = PushingTransportWrapper(LineAwareTransportWrapper(transport), timeout=5.0)

		self._send_queue_active = True
		self._sending_thread = threading.Thread(target=self._send_loop, name=b"comm.sending_thread")
		self._sending_thread.daemon = True
		self._sending_thread.start()

		super(ReprapGcodeProtocol, self).connect(transport,
		                                         transport_args=transport_args,
		                                         transport_kwargs=transport_kwargs)


	def disconnect(self, error=False):
		# TODO either clear send queue, or wait until everything has been sent...
		self._send_queue_active = False
		self._sending_thread = None

		if self._temperature_poller:
			self._temperature_poller.cancel()
			self._temperature_poller = None

		super(ReprapGcodeProtocol, self).disconnect()

	def process(self, job, position=0, tags=None):
		if isinstance(job, LocalGcodeStreamjob):
			self._internal_state["only_from_job"] = True
			self._internal_state["trigger_events"] = False
		else:
			self._internal_state["only_from_job"] = False
			self._internal_state["trigger_events"] = True

		super(ReprapGcodeProtocol, self).process(job, position=position, tags=tags)

	def move(self, x=None, y=None, z=None, e=None, feedrate=None, relative=False):
		commands = [self.flavor.command_move(x=x, y=y, z=z, e=e, f=feedrate)]

		if relative:
			commands = [self.flavor.command_set_relative_positioning()]\
			           + commands\
			           + [self.flavor.command_set_absolute_positioning()]

		self._send_commands(*commands)

	def home(self, x=False, y=False, z=False):
		self._send_commands(self.flavor.command_home(x=x, y=y, z=z))

	def change_tool(self, tool):
		self._send_commands(self.flavor.command_set_tool(tool))

	def set_feedrate_multiplier(self, multiplier):
		self._send_commands(self.flavor.command_set_feedrate_multiplier(multiplier))

	def set_extrusion_multiplier(self, multiplier):
		self._send_commands(self.flavor.command_set_extrusion_multiplier(multiplier))

	def set_extruder_temperature(self, temperature, tool=None, wait=False):
		self._send_commands(self.flavor.command_set_extruder_temp(temperature, tool, wait=wait))

	def set_bed_temperature(self, temperature, wait=False):
		self._send_commands(self.flavor.command_set_bed_temp(temperature, wait=wait))

	##~~ MotorControlProtocolMixin

	def set_motor_state(self, enabled):
		self._send_commands(self.flavor.command.set_motor_state(enabled))

	##~~ FanControlProtocolMixin

	def set_fan_speed(self, speed):
		self._send_commands(self.flavor.command.command_set_fan_speed(speed))

	##~~ FileStreamingProtocolMixin

	def init_file_storage(self):
		self._send_commands(self.flavor.command_sd_init())

	def list_files(self):
		self._send_commands(self.flavor.command_sd_refresh())

	def start_file_print(self, name, position=0, **kwargs):
		tags = kwargs.get(b"tags", set()) | {"trigger:protocol.start_file_print"}
		self._send_commands(self.flavor.command_sd_select_file(name),
		                    self.flavor.command_sd_set_pos(position),
		                    self.flavor.command_sd_start(),
		                    tags=tags)

	def pause_file_print(self):
		self._send_commands(self.flavor.command_sd_pause())

	def resume_file_print(self):
		self._send_commands(self.flavor.command_sd_resume())

	def delete_file(self, name):
		self._send_commands(self.flavor.command_sd_delete(name))

	def record_file(self, name, job):
		self._send_commands(self.flavor.command_sd_begin_write(name))

	def stop_recording_file(self):
		self._send_commands(self.flavor.command_sd_end_write())

	def get_file_print_status(self):
		self._send_commands(self.flavor.command_sd_status())

	def can_send(self):
		return not self._internal_state["long_running_command"] and not self._internal_state["heating"]

	def send_commands(self, *commands, **kwargs):
		command_type = kwargs.get(b"command_type")
		on_sent = kwargs.get(b"on_sent")
		tags = kwargs.get(b"tags")
		force = kwargs.get(b"force", False)

		# TODO job on hold
		if self.state == ProtocolState.PROCESSING and not self._job.runs_parallel and not force:
			if len(commands) > 1:
				command_type = None
			for command in commands:
				if not isinstance(command, Command):
					command = to_command(command, type=command_type, tags=tags)

				try:
					self._command_queue.put((command, on_sent), item_type=command_type)
					return True
				except TypeAlreadyInQueue as e:
					self._logger.debug("Type already in queue: " + e.type)
					return False
		elif self._is_operational() or force:
			self._send_commands(*commands, command_type=command_type, tags=tags)

	##~~ State handling

	def _is_operational(self):
		return self.state not in (ProtocolState.DISCONNECTED, ProtocolState.DISCONNECTED_WITH_ERROR)

	def _is_busy(self):
		return self.state in (ProtocolState.PROCESSING, ProtocolState.PAUSED)

	def _is_streaming(self):
		# TODO make more abstract?
		return self._is_busy() and self._job and isinstance(self._job, LocalGcodeStreamjob)

	def _on_state_connecting(self, old_state):
		if old_state is not ProtocolState.DISCONNECTED:
			return

		hello = self.flavor.command_hello()
		if hello:
			self._send_command(hello)
			self._clear_to_send.set()

	def _on_state_connected(self, old_state):
		if old_state == ProtocolState.CONNECTING:
			self._internal_state["timeout"] = self._get_timeout("communication")

			self._send_command(self.flavor.command_set_line(0))
			self._clear_to_send.set()

			def poll_temperature_interval():
				return self.interval["temperature_printing"] if self.state == ProtocolState.PROCESSING else self.interval["temperature_idle"]

			def poll_temperature():
				if self._is_operational() \
						and not self._internal_state["temperature_autoreporting"] \
						and not self._internal_state["only_from_job"] \
						and self.can_send():
					self._send_command(self.flavor.command_get_temp(), command_type="temperature_poll")

			from octoprint.util import RepeatedTimer
			self._temperature_poller = RepeatedTimer(poll_temperature_interval,
			                                         poll_temperature,
			                                         run_first=True)
			self._temperature_poller.start()

	def _on_state_disconnected(self, old_state):
		self._handle_disconnected(old_state, False)

	def _on_state_disconnected_with_error(self, old_state):
		self._handle_disconnected(old_state, True)

	def _handle_disconnected(self, old_state, error):
		if self._temperature_poller is not None:
			self._temperature_poller.cancel()
			self._temperature_poller = None

	##~~ Transport logging

	def on_transport_log_message(self, transport, data):
		super(ReprapGcodeProtocol, self).on_transport_log_message(transport, data)
		self._terminal_log.append("--- " + to_unicode(data.strip(), errors="replace"))

	def on_transport_log_received_data(self, transport, data):
		super(ReprapGcodeProtocol, self).on_transport_log_received_data(transport, data)
		self._terminal_log.append("<<< " + to_unicode(data.strip(), errors="replace"))

	def on_transport_log_sent_data(self, transport, data):
		super(ReprapGcodeProtocol, self).on_transport_log_sent_data(transport, data)
		self._terminal_log.append(">>> " + to_unicode(data.strip(), errors="replace"))

	##~~ Receiving

	def on_transport_data_pushed(self, transport, data):
		if transport != self._transport:
			return
		self._receive(data)

	def _receive(self, data):
		if data is None:
			# EOF
			# TODO handle EOF
			pass

		def convert_line(line):
			if line is None:
				return None, None
			stripped_line = line.strip().strip("\0")
			return stripped_line, stripped_line.lower()

		orig_line = to_unicode(data, encoding="ascii", errors="replace").strip()
		line, lower_line = convert_line(orig_line)

		if len(line):
			# TODO handle dwelling
			pass

		any_processed = False

		for message in self._registered_messages: # flavor.comm_* + flavor.message_*
			handler_method = getattr(self, "_on_{}".format(message), None)
			if not handler_method:
				# no handler, nothing to do
				continue

			# match line against flavor specific matcher
			matches = getattr(self.flavor, message)(line, lower_line, self._protected_state)
			continue_further = False
			if isinstance(matches, tuple) and len(matches) == 2:
				matches, continue_further = matches

			if matches:
				if not any_processed:
					self._on_comm_any()
					any_processed = True

				message_args = dict()

				parse_method = getattr(self.flavor, "parse_{}".format(message), None)
				if parse_method:
					# flavor specific parser? run it
					parse_result = parse_method(line, lower_line, self._protected_state)
					if parse_result is None:
						if continue_further:
							continue
						else:
							break

					# add parser result to kwargs
					message_args.update(parse_result)

				# before handler: flavor.before_comm_* or flavor.before_message_*
				before_handler = getattr(self.flavor, "before_{}".format(message), None)
				if before_handler:
					before_handler(**message_args)

				# run handler
				result = handler_method(**message_args)

				# after handler: flavor.after_comm_* or flavor.after_message_*
				after_handler = getattr(self.flavor, "after_{}".format(message), None)
				if after_handler:
					after_handler(result, **message_args)

				if not result:
					# nothing returned? handled -> only continue further if instructed
					if continue_further:
						continue
					else:
						break
		else:
			# unknown message
			pass

	def _on_comm_any(self):
		if self.state == ProtocolState.CONNECTING:
			hello = self.flavor.command_hello()
			if hello:
				self._send_command(hello)
				self._clear_to_send.set()

	def _on_comm_timeout(self):
		if self.state not in (ProtocolState.PROCESSING,
		                      ProtocolState.PAUSED,
		                      ProtocolState.CONNECTED):
			return

		general_message = "Configure long running commands or increase communication timeout if that happens regularly on specific commands or long moves."

		# figure out which consecutive timeout maximum we have to use
		if self._internal_state["long_running_command"]:
			consecutive_max = 5 # TODO take from config
		elif self.state in (ProtocolState.PROCESSING, ProtocolState.PAUSING, ProtocolState.CANCELLING):
			consecutive_max = 10 # TODO take from config
		else:
			consecutive_max = 15 # TODO take from config

		# now increment the timeout counter
		self._internal_state["timeout_consecutive"] += 1
		self._logger.debug("Now at {} consecutive timeouts".format(self._internal_state["timeout_consecutive"]))

		if 0 < consecutive_max < self._internal_state["timeout_consecutive"]:
			# too many consecutive timeouts, we give up
			# TODO implement
			pass

		elif self._internal_state["resend_requested"] is not None:
			message = "Communication timeout during an active resend, resending same line again to trigger response from printer."
			self._logger.info(message)
			self.notify_listeners("on_protocol_log", self, message + " " + general_message)
			if self._send_same_from_resend():
				self._clear_to_send.set()

		elif self._internal_state["heating"]:
			# blocking heatup active, consider that finished
			message = "Timeout while in an active heatup, considering heatup to be over"
			self._logger.info(message)
			self._finish_heatup()

		elif self._internal_state["long_running_command"]:
			# long running command active, ignore timeout
			self._logger.debug("Ran into a communication timeout, but a command known to be a long runner is currently active")

		elif self.state in (ProtocolState.PROCESSING, ProtocolState.PAUSED):
			# printing, try to tickle the printer
			message = "Communication timeout while printing, trying to trigger response from printer."
			self._logger.info(message)
			self.notify_listeners("on_protocol_log_message", self, message + " " + general_message)
			if self._send_command("M105", command_type="temperature"):
				self._clear_to_send.set()

		elif self._clear_to_send.blocked():
			# timeout while idle and no oks left, let's try to tickle the printer
			message = "Communication timeout while idle, trying to trigger response from printer."
			self._logger.info(message)
			self.notify_listeners("on_protocol_log_message", self, message + " " + general_message)
			self._clear_to_send.set()

		return self.state != ProtocolState.PROCESSING

	def _on_comm_ok(self):
		if self._internal_state["ignore_ok"] > 0:
			self._internal_state["ignore_ok"] -= 1
			if self._internal_state["ignore_ok"] < 0:
				self._internal_state["ignore_ok"] = 0
			self._logger.debug("Ignoring this ok, ignore counter is now {}".format(self._internal_state["ignore_ok"]))
			return

		if self.state == ProtocolState.CONNECTING:
			self.state = ProtocolState.CONNECTED
			return

		self._clear_to_send.set()

		# reset long running commands, persisted current tools and heatup counters on ok

		self._internal_state["long_running_command"] = False

		if self._internal_state["former_tool"]:
			self._internal_state["current_tool"] = self._internal_state["former_tool"]
			self._internal_state["former_tool"] = None

		self._finish_heatup()

		if not self.state in (ProtocolState.PROCESSING, ProtocolState.CONNECTED, ProtocolState.PAUSED):
			return

		# process ongoing resend requests and queues if we are operational

		if self._internal_state["resend_requested"] is not None:
			self._send_next_from_resend()
		else:
			self._continue_sending()

	def _on_comm_ignore_ok(self):
		self._internal_state["ignore_ok"] += 1
		self._logger.info("Ignoring next ok, counter is now at {}".format(self._internal_state["ignore_ok"]))

	def _on_comm_wait(self):
		if self.state == ProtocolState.PROCESSING:
			self._on_comm_ok()

	def _on_comm_resend(self, linenumber):
		if linenumber is None:
			return False

		if self._internal_state["resend_requested"] is None and linenumber == self._current_linenumber:
			# We don't expect to have an active resend request and the printer is requesting a resend of
			# a line we haven't yet sent.
			#
			# This means the printer got a line from us with N = self._current_linenumber - 1 but had already
			# acknowledged that. This can happen if the last line was resent due to a timeout during an active
			# (prior) resend request.
			#
			# We will ignore this resend request and just continue normally.
			self._logger.debug("Ignoring resend request for line {} == current line, we haven't sent that yet so the printer got N-1 twice from us, probably due to a timeout".format(linenumber))
			return

		last_communication_error = self._last_communication_error
		self._last_communication_error = None

		if last_communication_error is not None \
				and last_communication_error == "linenumber" \
				and linenumber == self._internal_state["resend_requested"] \
				and self._internal_state["resend_count"] < self._current_linenumber - linenumber - 1:
			self._logger.debug("Ignoring resend request for line {}, that still originates from lines we sent before we got the first resend request".format(linenumber))
			self._internal_state["resend_count"] += 1
			return

		if not linenumber in self._last_lines:
			self._logger.error("Printer requested to resend a line we don't have: {}".format(linenumber))
			if self._is_busy():
				# TODO set error state & log
				self.cancel_processing(error=True)
			else:
				return

		self._internal_state["resend_requested"] = linenumber
		self._internal_state["resend_linenumber"] = linenumber
		self._internal_state["resend_count"] = 0

		# if we log resends, make sure we don't log more resends than the set rate within a window
		#
		# this it to prevent the log from getting flooded for extremely bad communication issues
		if self._log_resends:
			now = monotonic_time()
			new_rate_window = self._log_resends_rate_start is None or self._log_resends_rate_start + self._log_resends_rate_frame < now
			in_rate = self._log_resends_rate_count < self._log_resends_max

			if new_rate_window or in_rate:
				if new_rate_window:
					self._log_resends_rate_start = now
					self._log_resends_rate_count = 0

				self._to_logfile_with_terminal(u"Got a resend request from the printer: requested line = {}, "
				                               u"current line = {}".format(linenumber, self._current_linenumber))
				self._log_resends_rate_count += 1

		self._send_queue.resend_active = True

	def _on_comm_start(self):
		if self.state == ProtocolState.CONNECTING:
			self.state = ProtocolState.CONNECTED

	def _on_comm_error(self, line, lower_line):
		for message in self._error_messages:
			handler_method = getattr(self, "_on_{}".format(message), None)
			if not handler_method:
				continue

			if getattr(self.flavor, message)(line, lower_line, self._protected_state):
				message_args = dict()

				parse_method = getattr(self.flavor, "parse_{}".format(message), None)
				if parse_method:
					parse_result = parse_method(line, lower_line, self._protected_state)
					if parse_result is None:
						break

					message_args.update(parse_result)

				if not handler_method(**message_args):
					break
		else:
			# unknown error message
			pass

	def _on_error_communication(self, error_type):
		self._last_communication_error = error_type

	# TODO multiline error handling

	def _on_message_temperature(self, max_tool_num, temperatures, heatup_detected):
		if heatup_detected:
			self._logger.debug("Externally triggered heatup detected")
			self._internal_state["heating"] = True
			self._internal_state["heatup_start"] = monotonic_time()

		potential_tools = dict(("T{}".format(x), "tool{}".format(x)) for x in range(max_tool_num + 1))
		potential_tools.update(dict(B="bed"))

		for tool_rep, tool in potential_tools.items():
			if not tool_rep in temperatures:
				continue
			actual, target = temperatures[tool_rep]
			self._set_temperature(tool, actual, target)

		self.notify_listeners("on_protocol_temperature", self, self._internal_state["temperatures"])

	def _on_message_firmware_info(self, firmware_name, data):
		# TODO
		#self._event_bus.fire(Events.FIRMWARE_DATA, dict(name=firmware_name, data=copy.copy(data)))

		if not self._internal_state["firmware_identified"] and firmware_name:
			self._logger.info("Printer reports firmware name \"{}\"".format(firmware_name))

			for flavor in all_flavors():
				if flavor.identifier and callable(flavor.identifier) and flavor.identifier(firmware_name, data):
					self._logger.info("Detected firmware flavor \"{}\", switching...".format(flavor.key))
					self._switch_flavor(flavor)

			self._internal_state["firmware_identified"] = True
			self._internal_state["firmware_name"] = firmware_name
			self._internal_state["firmware_info"] = data

	def _on_message_firmware_capability(self, cap, enabled):
		self._internal_state["firmware_capabilities"][cap] = enabled

		if self._capability_support.get(cap, False):
			if cap == CAPABILITY_AUTOREPORT_TEMP and enabled:
				self._logger.info("Firmware states that it supports temperature autoreporting")
				self._set_autoreport_temperature_interval()
			elif cap == CAPABILITY_AUTOREPORT_SD_STATUS and enabled:
				self._logger.info("Firmware states that it supports sd status autoreporting")
				self._set_autoreport_sdstatus_interval()

	def _on_message_sd_init_ok(self):
		self._internal_state["sd_available"] = True
		self.list_files()

	def _on_message_sd_init_fail(self):
		self._internal_state["sd_available"] = False

	def _on_message_sd_begin_file_list(self):
		self._internal_state["sd_files_temp"] = []

	def _on_message_sd_end_file_list(self):
		self._internal_state["sd_files"] = self._internal_state["sd_files_temp"]
		self._internal_state["sd_files_temp"] = None
		self.notify_listeners("on_protocol_sd_file_list", self, self._internal_state["sd_files"])

	def _on_message_sd_entry(self, name, size):
		self._internal_state["sd_files_temp"].append((name, size))

	def _on_message_sd_file_opened(self, name, size):
		self.notify_listeners("on_protocol_file_print_started", self, name, size)

	def _on_message_sd_done_printing(self):
		self.notify_listeners("on_protocol_file_print_done", self)

	def _on_message_sd_printing_byte(self, current, total):
		self.notify_listeners("on_protocol_sd_status", self, current, total)

	def _finish_heatup(self):
		if self._internal_state["heating"]:
			if self._internal_state["heating_start"]:
				self._internal_state["heating_lost"] = self._internal_state["heating_lost"] + (monotonic_time() - self._internal_state["heating_start"])
				self._internal_state["heating_start"] = None
			self._internal_state["heating"] = False

	##~~ Sending

	def _continue_sending(self):
		while self._active:
			if self.state in (ProtocolState.PROCESSING,) and not (self._job is None or not self._job.active or isinstance(self._job, SDFilePrintjob)):
				# we are printing, we really want to send either something from the command
				# queue or the next line from our file, so we only return here if we actually DO
				# send something
				if self._send_from_queue():
					# we found something in the queue to send
					return True

				# TODO job on hold
				#elif self.job_on_hold:
				#	return False

				elif self._send_next_from_job():
					# we sent the next line from the file
					return True

				self._logger.debug("No command sent on ok while printing, doing another iteration")
			else:
				# just send stuff from the command queue and be done with it
				return self._send_from_queue()

	def _send_same_from_resend(self):
		return self._send_next_from_resend(again=True)

	def _send_next_from_resend(self, again=False):
		self._last_communication_error = None

		# Make sure we are only handling one sending job at a time
		with self._send_queue_mutex:
			if again:
				# If we are about to send the last line from the active resend request
				# again, we first need to increment resend delta. It might already
				# be set to None if the last resend line was already sent, so
				# if that's the case we set it to 0. It will then be incremented,
				# the last line will be sent again, and then the delta will be
				# decremented and set to None again, completing the cycle.
				if self._internal_state["resend_linenumber"] is None:
					self._internal_state["resend_linenumber"] = self._current_linenumber
				self._internal_state["resend_linenumber"] -= 1

			linenumber = self._internal_state["resend_linenumber"]

			try:
				command = self._last_lines[linenumber]
			except KeyError:
				# TODO what to do?
				return False

			result = self._enqueue_for_sending(Command(command), linenumber=linenumber, processed=True, resend=True)

			self._internal_state["resend_linenumber"] += 1
			if self._internal_state["resend_linenumber"] == self._current_linenumber:
				self._internal_state["resend_requested"] = None
				self._internal_state["resend_linenumber"] = None
				self._internal_state["resend_count"] = 0

				self._send_queue.resend_active = False

			return result

	def _send_next_from_job(self):
		with self._send_queue_mutex:
			while self._active:
				# we loop until we've actually enqueued a line for sending
				if self.state != ProtocolState.PROCESSING:
					# we are no longer printing, return False
					return False

				# TODO job on hold
				#elif self.job_on_hold:
				#	# job is on hold, return false
				#	return False

				line = self._job.get_next()
				# TODO implement QueueMarker
				if line is None:
					# end of job, return False
					return False

				pos = self._job.pos
				read_lines = self._job.read_lines
				actual_lines = self._job.actual_lines
				command = to_command(line, tags={"source:file",
				                                 "filepos:{}".format(pos),
				                                 "fileline:{}".format(read_lines),
				                                 "fileactualline:{}".format(actual_lines)})
				if self._send_command(command):
					return True

				self._logger.debug("Command \"{}\" from job not enqueued, doing another iteration".format(line))

	def _send_from_queue(self):
		# We loop here to make sure that if we do NOT send the first command
		# from the queue, we'll send the second (if there is one). We do not
		# want to get stuck here by throwing away commands.
		while True:
			if self._internal_state["only_from_job"]:
				# irrelevant command queue => return
				return False

			try:
				entry = self._command_queue.get(block=False)
			except queue.Empty:
				# nothing in command queue
				return False

			try:
				if isinstance(entry, tuple):
					if not len(entry) == 3:
						# something with that entry is broken, ignore it and fetch
						# the next one
						continue
					cmd, callback = entry
				else:
					cmd = entry
					callback = None

				if self._send_command(cmd, on_sent=callback):
					# we actually did add this cmd to the send queue, so let's
					# return, we are done here
					return True
			finally:
				self._command_queue.task_done()

	def _send_commands(self, *commands, **kwargs):
		command_type = kwargs.get(b"command_type", None)
		result = False
		for command in commands:
			if len(commands) > 1:
				command_type = None
			result = self._send_command(command, command_type=command_type, tags=kwargs.get(b"tags", None)) or result
		return result

	def _send_command(self, command, command_type=None, tags=None, on_sent=None):
		if not isinstance(command, Command):
			command = to_command(command, type=command_type, tags=tags)

		with self._send_queue_mutex:
			if self._internal_state["trigger_events"]:
				results = self._process_command_phase("queuing", command)

				if not results:
					# command is no more, return
					return False
			else:
				results = [command]

			# process helper
			def process(command, on_sent=None):
				if command is None:
					# no command, next entry
					return False

				# process @ commands
				if isinstance(command, AtCommand):
					self._process_atcommand_phase("queuing", command)

				# actually enqueue the command for sending
				if self._enqueue_for_sending(command, on_sent=on_sent):
					if not self._is_streaming():
						# trigger the "queued" phase only if we are not streaming to sd right now
						self._process_command_phase("queued", command)

						if self._internal_state["trigger_events"] and isinstance(command, GcodeCommand) and command.code in GCODE_TO_EVENT:
							# TODO inject eventbus
							#self._eventbus.fire(gcode_to_event[command.code])
							pass
					return True
				else:
					return False

			# split off the final command, because that needs special treatment
			if len(results) > 1:
				last_command = results[-1]
				results = results[:-1]
			else:
				last_command = results[0]
				results = []

			# track if we enqueued anything at all
			enqueued_something = False

			# process all but the last ...
			for command in results:
				enqueued_something = process(command) or enqueued_something

			# ... and then process the last one with the on_sent callback attached
			command = last_command
			enqueued_something = process(command, on_sent=on_sent) or enqueued_something

			return enqueued_something

	def _enqueue_for_sending(self, command, linenumber=None, on_sent=None, processed=False, resend=False):
		"""
		Enqueues a command and optional command_type and linenumber in the send queue.

		Arguments:
		    command (Command): The command to send
		    linenumber (int): An optional line number to use for sending the
		        command.
		    on_sent (callable or None): Callback to call when the command has been sent
		    processed (bool): Whether this line has already been processed or not
		"""

		try:
			target = "send"
			if resend:
				target = "resend"

			self._send_queue.put((command, linenumber, on_sent, processed), item_type=command.type, target=target)
			return True
		except TypeAlreadyInQueue as e:
			self._logger.debug("Type already in queue: {}".format(e.type))
			return False

	def _send_loop(self):
		"""
		The send loop is reponsible of sending commands in ``self._send_queue`` over the line, if it is cleared for
		sending (through received ``ok`` responses from the printer's firmware.
		"""

		self._clear_to_send.wait()

		while self._send_queue_active:
			try:
				# wait until we have something in the queue
				entry = self._send_queue.get()

				try:
					# make sure we are still active
					if not self._send_queue_active:
						break

					# fetch command and optional linenumber from queue
					command, linenumber, on_sent, processed = entry

					if linenumber is not None:
						# line number predetermined - this only happens for resends, so we'll use the number and
						# send directly without any processing (since that already took place on the first sending!)
						self._do_send_with_checksum(to_str(command.line), linenumber)

					else:
						if not processed:
							results = self._process_command_phase("sending", command)

							if not results:
								# No, we are not going to send this, that was a last-minute bail.
								# However, since we already are in the send queue, our _monitor
								# loop won't be triggered with the reply from this unsent command
								# now, so we try to tickle the processing of any active
								# command queues manually
								self._continue_sending()

								# and now let's fetch the next item from the queue
								continue

							# we explicitly throw away plugin hook results that try
							# to perform command expansion in the sending/sent phase,
							# so "results" really should only have more than one entry
							# at this point if our core code contains a bug
							assert len(results) == 1

							# we only use the first (and only!) entry here
							command = results[0]

						if command.line.strip() == "":
							self._logger.info("Refusing to send an empty line to the printer")

							# same here, tickle the queues manually
							self._continue_sending()

							# and fetch the next item
							continue

						# handle @ commands
						if isinstance(command, AtCommand):
							self._process_command_phase("sending", command)

							# tickle...
							self._continue_sending()

							# ... and fetch the next item
							continue

						# now comes the part where we increase line numbers and send stuff - no turning back now
						if not self._transport.message_integrity:
							# transport does not have message integrity, let's see if we'll send a checksum
							command_requiring_checksum = isinstance(command, GcodeCommand) and command.code in self.flavor.checksum_requiring_commands
							command_allowing_checksum = isinstance(command, GcodeCommand) or self.flavor.unknown_with_checksum
							checksum_enabled = self.flavor.always_send_checksum or (self.state == ProtocolState.PROCESSING and not self.flavor.never_send_checksum)

							send_with_checksum = command_requiring_checksum or (command_allowing_checksum and checksum_enabled)

						else:
							# transport has message integrity, no checksum
							send_with_checksum = False

						command_to_send = command.line.encode("ascii", errors="replace")
						if send_with_checksum:
							self._do_increment_and_send_with_checksum(to_str(command_to_send))
						else:
							self._do_send_without_checksum(to_str(command_to_send))

					# trigger "sent" phase and use up one "ok"
					if on_sent is not None and callable(on_sent):
						# we have a sent callback for this specific command, let's execute it now
						on_sent()
					self._process_command_phase("sent", command)

					# we only need to use up a clear if the command we just sent was either a gcode command or if we also
					# require ack's for unknown commands
					use_up_clear = self.flavor.unknown_requires_ack
					if isinstance(command, GcodeCommand):
						use_up_clear = True

					if use_up_clear:
						# if we need to use up a clear, do that now
						self._clear_to_send.clear()
					else:
						# Otherwise we need to tickle the read queue - there might not be a reply
						# to this command, so our _monitor loop will stay waiting until timeout. We
						# definitely do not want that, so we tickle the queue manually here
						self._continue_sending()

				finally:
					# no matter _how_ we exit this block, we signal that we
					# are done processing the last fetched queue entry
					self._send_queue.task_done()

				# now we just wait for the next clear and then start again
				self._clear_to_send.wait()
			except:
				self._logger.exception("Caught an exception in the send loop")
		self.notify_listeners("on_protocol_log", self, "Closing down send loop")

	def _log_command_phase(self, phase, command):
		if self._phase_logger.isEnabledFor(logging.DEBUG):
			self._phase_logger.debug("phase: {}, command: {!r}".format(phase, command))

	def _process_command_phase(self, phase, command, **kwargs):
		command = to_command(command, **kwargs)
		results = [command]

		self._log_command_phase(phase, command)

		if phase not in ("queueing", "queued", "sending", "sent"):
			return results

		# send it through the phase specific handlers provided by plugins
		for name, hook in self._gcode_hooks[phase].items():
			new_results = []
			for command in results:
				try:
					hook_results = hook(self,
					                    phase,
					                    str(command),
					                    command.type,
					                    command.code if isinstance(command, GcodeCommand) else None,
					                    subcode=command.subcode if isinstance(command, GcodeCommand) else None,
					                    tags=command.tags)
				except:
					self._logger.exception("Error while processing hook {name} for phase {phase} and command {command}:".format(**locals()))
				else:
					normalized = normalize_command_handler_result(command, hook_results,
					                                              tags_to_add={"source:rewrite",
					                                                           "phase:{}".format(phase),
					                                                           "plugin:{}".format(name)})

					# make sure we don't allow multi entry results in anything but the queuing phase
					if not phase in ("queuing",) and len(normalized) > 1:
						self._logger.error("Error while processing hook {name} for phase {phase} and command {command}: Hook returned multi-entry result for phase {phase} and command {command}. That's not supported, if you need to do multi expansion of commands you need to do this in the queuing phase. Ignoring hook result and sending command as-is.".format(**locals()))
						new_results.append(command)
					else:
						new_results += normalized
			if not new_results:
				# hook handler returned None or empty list for all commands, so we'll stop here and return a full out empty result
				return []
			results = new_results

		# if it's a gcode command send it through the specific handler if it exists
		new_results = []
		modified = False
		for command in results:
			if isinstance(command, GcodeCommand):
				gcode_handler = "_gcode_" + command.code + "_" + phase
				if gcode_handler in self._handlers_gcode:
					# noinspection PyCallingNonCallable
					handler_results = getattr(self, gcode_handler)(command)
					new_results += normalize_command_handler_result(command, handler_results)
					modified = True
				else:
					new_results.append(command)
			else:
				new_results.append(command)

		if modified:
			if not new_results:
				# gcode handler returned None or empty list for all commands, so we'll stop here and return a full out empty result
				return []
			else:
				results = new_results

		# send it through the phase specific command handler if it exists
		command_phase_handler = "_command_phase_" + phase
		if command_phase_handler in self._handlers_command_phase:
			new_results = []
			for command in results:
				handler_results = getattr(self, command_phase_handler)(command)
				new_results += normalize_command_handler_result(command, handler_results)
			results = new_results

		# finally return whatever we resulted on
		return results

	def _process_atcommand_phase(self, phase, command):
		if (self._is_streaming()) or phase not in ("queuing", "sending"):
			return

		# send it through the phase specific handlers provided by plugins
		for name, hook in self._atcommand_hooks[phase].items():
			try:
				hook(self, phase, command.atcommand, command.parameters, tags=command.tags)
			except:
				self._logger.exception("Error while processing hook {} for phase {} and command {}:".format(name, phase, command.atcommand))

		# trigger built-in handler if available
		atcommand_handler = "_atcommand_{}_{}".format(command.atcommand, phase)
		if atcommand_handler in self._handlers_atcommand:
			try:
				getattr(self, atcommand_handler)(command)
			except:
				self._logger.exception("Error in handler for phase {} and command {}".format(phase, command.atcommand))

	##~~ actual sending via serial

	def _do_increment_and_send_with_checksum(self, line):
		"""
		Args:
			line (str): the line to send
		"""
		with self._line_mutex:
			line_number = self._current_linenumber
			self._add_to_last_lines(line, line_number=line_number)
			self._current_linenumber += 1
			self._do_send_with_checksum(line, line_number)

	def _do_send_with_checksum(self, line, line_number):
		"""
		Args:
			line (str): the line to send
			line_number (int): the line number to send
		"""
		command_to_send = b"N" + str(line_number) + b" " + line
		checksum = 0
		for c in bytearray(command_to_send):
			checksum ^= c
		command_to_send = command_to_send + b"*" + str(checksum)
		self._do_send_without_checksum(command_to_send)

	def _do_send_without_checksum(self, data):
		"""
		Args:
			data (str): the data to send
		"""
		self._transport.write(data + b"\n")

	def _add_to_last_lines(self, command, line_number=None):
		self._last_lines.append(command, line_number=line_number)

	##~~ gcode command handlers

	def _gcode_T_sent(self, command):
		if command.tool:
			self._internal_state["current_tool"] = int(command.tool)

	def _gcode_G0_sent(self, command):
		if command.z is not None and self._internal_state["current_z"] != command.z:
				self._internal_state["current_z"] = command.z
				self.notify_listeners("on_protocol_position_z_update", self, self._internal_state["current_z"])
	_gcode_G1_sent = _gcode_G0_sent

	def _gcode_M0_queuing(self, command):
		self.pause_file_print()
		return None, # Don't send the M0 or M1 to the machine, as M0 and M1 are handled as an LCD menu pause.
	_gcode_M1_queuing = _gcode_M0_queuing

	def _gcode_M25_queuing(self, command):
		# M25 while not printing from SD will be handled as pause. This way it can be used as another marker
		# for GCODE induced pausing. Send it to the printer anyway though.
		if self.state == ProtocolState.PROCESSING and not isinstance(self._job, SDFilePrintjob):
			self.pause_file_print()

	def _gcode_M140_queuing(self, command):
		# TODO heated bed handling needs heated bed flag from printer profile
		#if not self._printerProfileManager.get_current_or_default()["heatedBed"]:
		#	self._log("Warn: Not sending \"{}\", printer profile has no heated bed".format(cmd))
		#	return None, # Don't send bed commands if we don't have a heated bed
		pass
	_gcode_M190_queuing = _gcode_M140_queuing

	def _gcode_M155_sending(self, command):
		try:
			interval = int(command.s)
			self._internal_state["temperature_autoreporting"] = self._internal_state["firmware_capabilities"].get(CAPABILITY_AUTOREPORT_TEMP,
			                                                                                                      False) \
			                                                    and (interval > 0)
		except:
			pass

	def _gcode_M27_sending(self, command):
		try:
			interval = int(command.s)
			self._internal_state["sd_status_autoreporting"] = self._internal_state["firmware_capabilities"].get(CAPABILITY_AUTOREPORT_SD_STATUS,
			                                                                                                    False) \
			                                                  and (interval > 0)
		except:
			pass

	def _gcode_M104_sent(self, command, wait=False, support_r=False):
		tool_num = self._internal_state["current_tool"]
		if command.t:
			tool_num = command.t

			if wait:
				self._internal_state["former_tool"] = self._internal_state["current_tool"]
				self._internal_state["current_tool"] = tool_num

		target = None
		if command.s is not None:
			target = float(command.s)
		elif command.r is not None and support_r:
			target = float(command.r)

		if target:
			tool = "tool{}".format(tool_num)
			self._set_temperature(tool, None, target)
			self.notify_listeners("on_protocol_temperature", self, self._internal_state["temperatures"])

	def _gcode_M140_sent(self, command, wait=False, support_r=False):
		target = None
		if command.s is not None:
			target = float(command.s)
		elif command.r is not None and support_r:
			target = float(command.r)

		if target:
			self._set_temperature("bed", None, target)
			self.notify_listeners("on_protocol_temperature", self, self._internal_state["temperatures"])

	def _gcode_M109_sent(self, command):
		self._internal_state["heatup_start"] = monotonic_time()
		self._internal_state["long_running_command"] = True
		self._internal_state["heating"] = True
		self._gcode_M104_sent(command, wait=True, support_r=True)

	def _gcode_M190_sent(self, command):
		self._internal_state["heatup_start"] = monotonic_time()
		self._internal_state["long_running_command"] = True
		self._internal_state["heating"] = True
		self._gcode_M140_sent(command, wait=True, support_r=True)

	def _gcode_M116_sent(self, command):
		self._internal_state["heatup_start"] = monotonic_time()
		self._internal_state["long_running_command"] = True
		self._internal_state["heating"] = True

	def _gcode_M110_sending(self, command):
		new_line_number = None
		if command.n:
			try:
				new_line_number = int(command.n)
			except:
				pass
		else:
			new_line_number = 0

		with self._line_mutex:
			self._logger.info("M110 detected, setting current line number to {}".format(new_line_number))

			# send M110 command with new line number
			self._current_linenumber = new_line_number

			# after a reset of the line number we have no way to determine what line exactly the printer now wants
			self._last_lines.clear()
		self._internal_state["resend_linenumber"] = None

	def _gcode_M112_queuing(self, command):
		# emergency stop, jump the queue with the M112
		self._do_send_with_checksum(str(self.flavor.command_emergency_stop()))
		self._do_increment_and_send_with_checksum(str(self.flavor.command_emergency_stop()))

		# No idea if the printer is still listening or if M112 won. Just in case
		# we'll now try to also manually make sure all heaters are shut off - better
		# safe than sorry. We do this ignoring the queue since at this point it
		# is irrelevant whether the printer has sent enough ack's or not, we
		# are going to shutdown the connection in a second anyhow.
		# TODO needs printer profile
		"""
		for tool in range(self._printerProfileManager.get_current_or_default()["extruder"]["count"]):
			self._do_increment_and_send_with_checksum(self.flavor.set_extruder_temp(s=0, t=tool))
		if self._printerProfileManager.get_current_or_default()["heatedBed"]:
			self._do_increment_and_send_with_checksum(str(self.flavor.set_bed_temp(s=0)))
		"""

		# close to reset host state
		# TODO needs error and event handling
		"""
		self._errorValue = "Closing serial port due to emergency stop M112."
		self._log(self._errorValue)
		self.close(is_error=True)

		# fire the M112 event since we sent it and we're going to prevent the caller from seeing it
		gcode = "M112"
		if gcode in gcodeToEvent:
			eventManager().fire(gcodeToEvent[gcode])
		"""

		# return None 1-tuple to eat the one that is queuing because we don't want to send it twice
		# I hope it got it the first time because as far as I can tell, there is no way to know
		return None,

	def _gcode_G4_sent(self, cmd):
		# we are intending to dwell for a period of time, increase the timeout to match
		timeout = 0
		if cmd.p:
			timeout = float(cmd.p) / 1000.0
		elif cmd.s:
			timeout = float(cmd.s)
		self._internal_state["timeout"] = self._get_timeout("communication") + timeout

	##~~ atcommand handlers

	def _atcommand_pause_queuing(self, command):
		tags = command.tags
		if tags is None:
			tags = set()
		if not "script:afterPrintPaused" in tags:
			self.pause_processing(tags={"trigger:atcommand_pause"})

	def _atcommand_cancel_queuing(self, command):
		tags = command.tags
		if tags is None:
			tags = set()
		if not "script:afterPrintCancelled" in tags:
			self.cancel_processing(tags={"trigger:atcommand_cancel"})
	_atcommand_abort_queuing = _atcommand_cancel_queuing

	def _atcommand_resume_queuing(self, command):
		tags = command.tags
		if tags is None:
			tags = set()
		if not "script:beforePrintResumed" in tags:
			self.resume_processing(tags={"trigger:atcommand_resume"})

	##~~ command phase handlers

	def _command_phase_sending(self, cmd, gcode=None):
		if gcode is not None and gcode.code in self.flavor.long_running_commands:
			self._internal_state["long_running_command"] = True

	##~~ autoreporting

	def _set_autoreport_temperature_interval(self, interval=None):
		if interval is None:
			try:
				interval = int(self.interval.get("temperature_autoreport", 2))
			except:
				interval = 2
		self.send_commands(self.flavor.command_autoreport_temperature(interval=interval))

	def _set_autoreport_sdstatus_interval(self, interval=None):
		if interval is None:
			try:
				interval = int(self.interval.get("sd_status_autoreport", 1))
			except:
				interval = 1
		self.send_commands(self.flavor.command_autoreport_sd_status(interval=interval))

	##~~ helpers

	def _set_temperature(self, tool, actual, target):
		temperatures = self._internal_state["temperatures"]
		if actual is None and tool in temperatures:
			actual, _ = temperatures[tool]
		if target is None and tool in temperatures:
			_, target = temperatures[tool]
		temperatures[tool] = (actual, target)

	def _get_timeout(self, timeout_type):
		if timeout_type in self.timeouts:
			return monotonic_time() + self.timeouts[timeout_type]
		else:
			return monotonic_time()

	def _to_logfile_with_terminal(self, message=None, level=logging.INFO):
		log = u"Last lines in terminal:\n" + u"\n".join(map(lambda x: u"| {}".format(x), list(self._terminal_log)))
		if message is not None:
			log = message + u"\n| " + log
		self._logger.log(level, log)

	def _switch_flavor(self, new_flavor):
		self.flavor = new_flavor

		flavor_comm_attrs = self.get_flavor_attributes_starting_with(self.flavor, "comm_")
		flavor_message_attrs = self.get_flavor_attributes_starting_with(self.flavor, "message_")
		flavor_error_attrs = self.get_flavor_attributes_starting_with(self.flavor, "error_")

		self._registered_messages = flavor_comm_attrs + flavor_message_attrs
		self._error_messages = flavor_error_attrs


if __name__ == "__main__":
	from octoprint.comm.transport.serialtransport import VirtualSerialTransport
	from octoprint.comm.protocol.reprap.virtual import VirtualPrinter
	from octoprint.comm.protocol.reprap.flavors import GenericFlavor
	from octoprint.comm.protocol import ProtocolListener, FileAwareProtocolListener
	from octoprint.job import PrintjobListener

	import os

	virtual_sd = "C:/Users/gina/AppData/Roaming/OctoPrint/virtualSd"
	filename = "50mm-t~5.gco"

	def virtual_serial_factory():
		return VirtualPrinter(virtual_sd=virtual_sd)

	class CustomProtocolListener(ProtocolListener, FileAwareProtocolListener, PrintjobListener):

		def __init__(self, protocol):
			super(CustomProtocolListener, self).__init__()
			self._job = None
			self._queue = []
			self._protocol = protocol

		def add_job(self, job):
			self._queue.append(job)

		def get_next_job(self):
			return self._queue.pop(0) if self._queue else None

		def start_next_job(self):
			job = self.get_next_job()
			if job is None:
				return

			self._job = job
			self._job.register_listener(self)
			self._protocol.process(self._job)

		def on_protocol_state(self, protocol, old_state, new_state):
			if protocol != self._protocol:
				return
			print("State changed from {} to {}".format(old_state, new_state))

		def on_protocol_temperature(self, protocol, temperatures):
			if protocol != self._protocol:
				return
			print("Temperature update: {!r}".format(temperatures))

		def on_protocol_sd_file_list(self, protocol, files):
			if protocol != self._protocol:
				return
			print("Received files from printer:")
			for f in files:
				print("\t{} (Size {} bytes)".format(*f))

			self.start_next_job()

		def on_protocol_log(self, protocol, data):
			if protocol != self._protocol:
				return
			print(data)

		def on_job_progress(self, job, progress, elapsed, estimated):
			if job != self._job:
				return
			print("Job progress: {}% (Time: {} min / {} min)".format(int(progress * 100) if progress is not None else "0",
			                                                       "{}:{}".format(int(elapsed / 60), int(elapsed % 60)) if elapsed is not None else "?",
			                                                       "{}:{}".format(int(estimated / 60), int(estimated % 60)) if estimated is not None else "?"))

		def on_job_done(self, job):
			self.start_next_job()

	transport = VirtualSerialTransport(virtual_serial_factory=virtual_serial_factory)
	protocol = ReprapGcodeProtocol()

	protocol_listener = CustomProtocolListener(protocol)
	protocol_listener.add_job(LocalGcodeFilePrintjob(os.path.join(virtual_sd, filename)))
	protocol_listener.add_job(SDFilePrintjob(filename))
	protocol.register_listener(protocol_listener)

	transport.connect()
	protocol.connect(transport)
"""

if __name__ == "__main__":
	gcode = GcodeCommand.from_line("M104 S220.4 T2")
"""