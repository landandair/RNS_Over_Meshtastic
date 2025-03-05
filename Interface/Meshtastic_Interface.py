# MIT License - Copyright (c) 2024 Mark Qvist / unsigned.io
# This example illustrates creating a custom interface
# definition, that can be loaded and used by Reticulum at
# runtime. Any number of custom interfaces can be created
# and loaded. To use the interface place it in the folder
# ~/.reticulum/interfaces, and add an interface entry to
# your Reticulum configuration file similar to this:

# [[Meshtastic Interface]]
# type = Meshtastic_Interface
# enabled = true
# mode = gateway
# port = / dev / ttyUSB0
# speed = 115200

import os
import sys
import struct
import threading
import time


# Let's define our custom interface class. It must
# be a subclass of the RNS "Interface" class.
class MeshtasticInterface(Interface):
    # All interface classes must define a default
    # IFAC size, used in IFAC setup when the user
    # has not specified a custom IFAC size. This
    # option is specified in bytes.
    DEFAULT_IFAC_SIZE = 8
    speed_to_delay = {8: .4,  # Short-range Turbo (recommended)
                      6: 1,  # Short Fast (best if short turbo is unavailable)
                      5: 3,  # Short-range Slow (best if short turbo is unavailable)
                      7: 12,  # Long Range - moderate Fast
                      4: 4,  # Medium Range - Fast  (Slowest recommended speed)
                      3:6,  # Medium Range - Slow
                      1: 15,  # Long Range - Slow
                      0: 8  # Long Range - Fast
                      }

    # The following properties are local to this
    # particular interface implementation.
    owner = None
    port = None
    speed = None
    databits = None
    parity = None
    stopbits = None
    serial = None

    # All Reticulum interfaces must have an __init__
    # method that takes 2 positional arguments:
    # The owner RNS Transport instance, and a dict
    # of configuration values.
    # noinspection PyUnboundLocalVariable
    def __init__(self, owner, configuration):

        # The following lines demonstrate handling
        # potential dependencies required for the
        # interface to function correctly.
        import importlib.util
        if importlib.util.find_spec('meshtastic') is not None:
            import meshtastic
            from meshtastic.ble_interface import BLEInterface
            from meshtastic.serial_interface import SerialInterface
            from pubsub import pub
            self.mt_bin_port = meshtastic.portnums_pb2.PRIVATE_APP
        else:
            RNS.log("Using this interface requires a meshtastic module to be installed.", RNS.LOG_CRITICAL)
            RNS.log("You can install one with the command: python3 -m pip install meshtastic", RNS.LOG_CRITICAL)
            RNS.panic()

        # We start out by initialising the super-class
        super().__init__()

        # To make sure the configuration data is in the
        # correct format, we parse it through the following
        # method on the generic Interface class. This step
        # is required to ensure compatibility on all the
        # platforms that Reticulum supports.
        ifconf = Interface.get_config_obj(configuration)

        # Read the interface name from the configuration
        # and set it on our interface instance.
        name = ifconf["name"]
        self.name = name

        # We read configuration parameters from the supplied
        # configuration data, and provide default values in
        # case any are missing.
        port = ifconf["port"] if "port" in ifconf else None
        ble_port = ifconf["ble_port"] if "ble_port" in ifconf else None
        tcp_port = ifconf["tcp_port"] if "tcp_port" in ifconf else None
        speed = int(ifconf["data_speed"]) if "data_speed" in ifconf else 8
        hop_limit = int(ifconf["hop_limit"]) if "hop_limit" in ifconf else 1

        # All interfaces must supply a hardware MTU value
        # to the RNS Transport instance. This value should
        # be the maximum data packet payload size that the
        # underlying medium is capable of handling in all
        # cases without any segmentation.
        self.HW_MTU = 564

        # We initially set the "online" property to false,
        # since the interface has not actually been fully
        # initialised and connected yet.
        self.online = False

        # In this case, we can also set the indicated bit-
        # rate of the interface to the serial port speed.
        self.bitrate = ifconf["bitrate"] if "bitrate" in ifconf else 500

        # Configure internal properties on the interface
        # according to the supplied configuration.
        self.owner = owner
        self.port = port
        self.ble_port = ble_port
        self.tcp_port = tcp_port
        self.speed = speed
        self.timeout = 100
        self.interface = None
        self.outgoing_packet_storage = {}
        self.packet_i_queue = []
        self.assembly_dict = {}
        self.expected_index = {}
        self.requested_index = {}
        self.packet_index = 0
        self.hop_limit = hop_limit


        pub.subscribe(self.process_message, "meshtastic.receive")
        pub.subscribe(self.connection_complete, "meshtastic.connection.established")
        pub.subscribe(self.connection_closed, "meshtastic.connection.lost")

        # Since all required parameters are now configured,
        # we will try opening the serial port.
        try:
            self.open_interface()
        except Exception as e:
            RNS.log("Meshtastic: Could not open meshtastic interface " + str(self), RNS.LOG_ERROR)
            raise e

        # If opening the port succeeded, run any post-open
        # configuration required.

    # Open the meshtastic interface with supplied configuration
    # parameters and store a reference to the open port.
    def open_interface(self):
        if self.port:
            RNS.log("Meshtastic: Opening serial port " + self.port + "...", RNS.LOG_VERBOSE)
            from meshtastic.serial_interface import SerialInterface
            self.interface = SerialInterface(devPath=self.port)
        elif self.ble_port:
            RNS.log("Meshtastic: Opening ble device " + self.ble_port + "...", RNS.LOG_VERBOSE)
            from meshtastic.ble_interface import BLEInterface
            self.interface = BLEInterface(address=self.ble_port)
        elif self.tcp_port:
            RNS.log("Meshtastic: Opening tcp device " + self.tcp_port + "...", RNS.LOG_VERBOSE)
            from meshtastic.tcp_interface import TCPInterface, DEFAULT_TCP_PORT
            host = self.tcp_port
            port = DEFAULT_TCP_PORT
            if ":" in self.tcp_port:
                host, port = self.tcp_port.split(":", maxsplit=1)
            
            self.interface = TCPInterface(hostname=host, portNumber=port)
        else:
            raise ValueError(f"No port or ble_port specified for {self}")

    # The only thing required after opening the port
    # is to wait a small amount of time for the
    # hardware to initialise and then start a thread
    # that reads any incoming data from the device.
    def configure_device(self, interface):
        # Set the speed to the radio
        ourNode = interface.getNode('^local')
        if ourNode.localConfig.lora.modem_preset != self.speed:
            ourNode.localConfig.lora.modem_preset = self.speed
            ourNode.writeConfig("lora")
            self.online = False
        else:
            thread = threading.Thread(target=self.write_loop)
            thread.daemon = True
            thread.start()
            self.online = True

    # This method will be called from our read-loop
    # whenever a full packet has been received over
    # the underlying medium.
    def process_incoming(self, data):
        # RNS.log(f'Data Received: {len(data)}')
        # Update our received bytes counter
        self.rxb += len(data)

        # And send the data packet to the Transport
        # instance for processing.
        self.owner.inbound(data, self)

    # The running Reticulum Transport instance will
    # call this method on the interface whenever the
    # interface must transmit a packet.
    def process_outgoing(self, data:bytes):
        if len(self.packet_i_queue) < 256:
            # Then write the framed data to the port
            handler = PacketHandler(data, self.packet_index)
            for key in handler.get_keys():
                self.packet_i_queue.append((handler.index, key))
            self.outgoing_packet_storage[handler.index] = handler
            self.packet_index = calc_index(self.packet_index)

    def process_message(self, packet, interface):
        """Process meshtastic traffic incoming to system"""
        # RNS.log(f'From: {packet["from"]}, payload: {packet["decoded"]["portnum"], packet["decoded"]["payload"]}')
        if "decoded" in packet:
            if packet["decoded"]["portnum"] == "PRIVATE_APP":
                if packet["from"] not in self.expected_index:
                    self.expected_index[packet["from"]] = []
                expected_index = self.expected_index[packet["from"]]
                if packet["from"] not in self.requested_index:
                    self.requested_index[packet["from"]] = []
                requested_index = self.requested_index[packet["from"]]
                if packet["from"] not in self.assembly_dict:
                    self.assembly_dict[packet["from"]] = {}
                payload = packet["decoded"]["payload"]
                packet_handler = PacketHandler()
                if payload[:3] == b'REQ':  # If it is a request
                    new_index, pos = packet_handler.get_metadata(payload[3:])
                    self.packet_i_queue.insert(0, (new_index, pos))
                else:  # Its data
                    new_index, pos = packet_handler.get_metadata(payload)
                    expect_followup = True
                    if (new_index, abs(pos)) in expected_index:
                        while (new_index, abs(pos)) in expected_index:
                            expected_index.remove((new_index, abs(pos)))
                    elif (new_index, abs(pos)) in requested_index:
                        requested_index.remove((new_index, abs(pos)))
                        expect_followup = False
                    elif len(expected_index):   # Packet was not expected but add last expected index and carry on
                        ex_index, ex_pos = expected_index.pop(0)
                        requested_index.append((ex_index, abs(ex_pos)))
                        if len(requested_index) > 10:  # Keep length under 10 per client
                            requested_index.pop(0)
                        self.packet_i_queue.insert(0, (-1, 0))
                        self.outgoing_packet_storage[-1] = [b'REQ' + struct.pack(packet_handler.struct_format,
                                                                                 ex_index, ex_pos)]

                    if new_index in self.assembly_dict[packet["from"]]:  # Old packet handler
                        old_handler = self.assembly_dict[packet["from"]][new_index]
                        data = old_handler.process_packet(payload)
                    else:  # Make a new one
                        data = packet_handler.process_packet(payload)
                        self.assembly_dict[packet["from"]][new_index] = packet_handler
                    if data:
                        self.process_incoming(data)
                        self.assembly_dict[packet["from"]].pop(new_index)
                    if pos < 0:  # Move to next index
                        expected = (calc_index(new_index), 1)
                    else:  # Move on to higher pos
                        expected = (new_index, (pos + 1))
                    if expect_followup:
                        expected_index.insert(0, expected)

        pass

    def write_loop(self):
        """Writes packets from queue to meshtastic device"""
        RNS.log('Meshtastic: outgoing loop started')
        sleep_time = self.speed_to_delay[self.speed] if self.speed in self.speed_to_delay else 7
        import meshtastic
        while True:
            data = None
            while not data and self.packet_i_queue:
                index, position = self.packet_i_queue.pop(0)
                if index in self.outgoing_packet_storage:
                    data = self.outgoing_packet_storage[index][position]  # Get data from position
            if data:
                # Update the transmitted bytes counter
                # and ensure that all data was written
                self.txb += len(data) - 2  # -2 for overhead
                # RNS.log(f'Sending: {data}')
                self.interface.sendData(data,
                                   portNum=meshtastic.portnums_pb2.PRIVATE_APP,
                                   wantAck=False,
                                   wantResponse=False,
                                   channelIndex=0,
                                   hopLimit=self.hop_limit)
            time.sleep(sleep_time)  # Make sending rate dynamic

    def connection_complete(self, interface):
        """Process meshtastic connection opened"""
        RNS.log("Meshtastic: Connected")
        self.configure_device(interface)
        self.interface = interface

    def connection_closed(self, interface):
        """Process meshtastic traffic incoming to system"""
        RNS.log("Meshtastic: Disconnected")
        self.online = False
        time.sleep(10)  # Wait before restarting
        self.open_interface()

    # Signal to Reticulum that this interface should
    # not perform any ingress limiting.
    @staticmethod
    def should_ingress_limit():
        return False

    # We must provide a string representation of this
    # interface, that is used whenever the interface
    # is printed in logs or external programs.
    def __str__(self):
        return "MeshtasticInterface[" + self.name + "]"

class PacketHandler:
    struct_format = 'Bb'

    def __init__(self, data=None, index=None, max_payload=200):
        self.max_payload = max_payload
        self.index = index
        self.data_dict = {}
        self.loop_pos = 1
        self.done = False
        if data:  # Means we are sending
            self.split_data(data)

    def split_data(self, data: bytes):
        """Split data into even chunks and add metadata to it"""
        data_list = []
        data_len = len(data)
        num_packets = data_len//self.max_payload + 1  # Number of packets needed to hold the data
        packet_size = data_len//num_packets + 1
        for i in range(0, data_len, packet_size):
            data_list.append(data[i:i + packet_size])
        for i, packet in enumerate(data_list):
            pos = i+1
            if pos == len(data_list):
                pos = -pos
            meta_data = struct.pack(self.struct_format, self.index, pos)
            self.data_dict[pos] = meta_data+packet

    def get_next(self):
        """get next packet to send"""
        ret = self[self.loop_pos]
        if max(self.data_dict.keys()) < self.loop_pos:
            self.loop_pos = 1
            self.done = True
        self.loop_pos += 1
        return ret

    def is_done(self):
        """Return True if the get_next loop is completed"""
        return self.done

    def __getitem__(self, i):
        """Get the packet at an index"""
        if i in self.data_dict:
            return self.data_dict[i]
        elif -i in self.data_dict:
            return self.data_dict[-i]

    def process_packet(self, packet: bytes):
        """Returns data if the packet is complete, and nothing if it isn't"""
        new_index, pos = self.get_metadata(packet)
        self.index = new_index
        self.data_dict[abs(pos)] = packet
        if pos < 0:
            return self.assemble_data()
        return None

    def check_data(self):
        """check content of data dict against the expected content"""
        expected = 1
        for key in sorted(self.data_dict.keys()):
            if key != expected:
                return False
            expected += 1
        return True

    def get_keys(self):
        return self.data_dict.keys()

    def assemble_data(self):
        """Put all the data together and return it or nothing if it fails"""
        if self.check_data():
            data = b''
            for key in sorted(self.data_dict.keys()):
                data += self.data_dict[key][struct.calcsize(self.struct_format):]
            return data
        else:
            return None

    def get_metadata(self, packet):
        # Get and return metadata from packet
        size = struct.calcsize(self.struct_format)
        meta_data = packet[:size]
        new_index, pos = struct.unpack(self.struct_format, meta_data)
        return new_index, pos


def calc_index(curr_index):
    return (curr_index + 1) % 256

# Finally, register the defined interface class as the
# target class for Reticulum to use as an interface
interface_class = MeshtasticInterface
