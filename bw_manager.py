#!/usr/bin/env python3

import hashlib
import ipaddress
import json
import logging
import random
import re
import socket
import struct
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"


# ============================================================================
# Constants
# ============================================================================

RADIUS_CODE_COA_REQUEST = 43
RADIUS_CODE_COA_ACK = 44
RADIUS_CODE_COA_NAK = 45

ATTR_USER_NAME = 1
ATTR_ERROR_CAUSE = 101

MIKROTIK_VENDOR_ID = 14988
MIKROTIK_RATE_LIMIT = 8

MAX_BURST_BPS = 999_000_000


# ============================================================================
# Logging
# ============================================================================

def setup_logging(config):
    log_file = config.get(
        "log_file",
        "/var/log/bw_manager/bw_manager.log"
    )

    log_path = Path(log_file)

    try:
        log_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )
    except Exception:
        pass

    handlers = [
        logging.StreamHandler()
    ]

    try:
        handlers.insert(
            0,
            logging.FileHandler(
                log_file,
                encoding="utf-8"
            )
        )
    except Exception:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers
    )


# ============================================================================
# Configuration
# ============================================================================

def load_config():
    with open(
        CONFIG_FILE,
        "r",
        encoding="utf-8"
    ) as file:
        return json.load(file)


def get_burst_time(config):
    try:
        burst_time = int(
            config.get(
                "burst_time_seconds",
                10
            )
        )
    except (TypeError, ValueError):
        burst_time = 10

    if burst_time < 1:
        burst_time = 1

    return burst_time


# ============================================================================
# RouterOS API helpers
# ============================================================================

def recv_exact(sock, size):
    data = b""

    while len(data) < size:
        chunk = sock.recv(
            size - len(data)
        )

        if not chunk:
            raise ConnectionError(
                "Connection closed while receiving data"
            )

        data += chunk

    return data


def ros_encode_length(length):
    if length < 0x80:
        return bytes([length])

    if length < 0x4000:
        value = length | 0x8000

        return struct.pack(
            ">H",
            value
        )

    if length < 0x200000:
        value = length | 0xC00000

        return bytes([
            (value >> 16) & 0xFF,
            (value >> 8) & 0xFF,
            value & 0xFF
        ])

    if length < 0x10000000:
        value = length | 0xE0000000

        return struct.pack(
            ">I",
            value
        )

    if length < 0x100000000:
        return (
            b"\xF0"
            + struct.pack(
                ">I",
                length
            )
        )

    raise ValueError(
        "RouterOS API word is too large"
    )


def ros_decode_length(sock):
    first = recv_exact(
        sock,
        1
    )[0]

    if first < 0x80:
        return first

    if first < 0xC0:
        second = recv_exact(
            sock,
            1
        )[0]

        return (
            ((first & 0x3F) << 8)
            | second
        )

    if first < 0xE0:
        data = recv_exact(
            sock,
            2
        )

        return (
            ((first & 0x1F) << 16)
            | (data[0] << 8)
            | data[1]
        )

    if first < 0xF0:
        data = recv_exact(
            sock,
            3
        )

        return (
            ((first & 0x0F) << 24)
            | (data[0] << 16)
            | (data[1] << 8)
            | data[2]
        )

    data = recv_exact(
        sock,
        4
    )

    return struct.unpack(
        ">I",
        data
    )[0]


def ros_send_word(sock, word):
    if isinstance(word, str):
        word = word.encode(
            "utf-8"
        )

    sock.sendall(
        ros_encode_length(
            len(word)
        )
    )

    sock.sendall(
        word
    )


def ros_send_sentence(sock, words):
    for word in words:
        ros_send_word(
            sock,
            word
        )

    sock.sendall(
        b"\x00"
    )


def ros_read_word(sock):
    length = ros_decode_length(
        sock
    )

    if length == 0:
        return None

    return recv_exact(
        sock,
        length
    )


def ros_read_sentence(sock):
    words = []

    while True:
        word = ros_read_word(
            sock
        )

        if word is None:
            break

        words.append(
            word
        )

    return words


def ros_sentence_to_dict(words):
    result = {}

    for word in words:
        try:
            text = word.decode(
                "utf-8",
                errors="replace"
            )
        except Exception:
            continue

        if not text.startswith("="):
            continue

        text = text[1:]

        if "=" not in text:
            continue

        key, value = text.split(
            "=",
            1
        )

        result[key] = value

    return result


def ros_response_to_text(response):
    result = []

    for word in response:
        try:
            result.append(
                word.decode(
                    "utf-8",
                    errors="replace"
                )
            )
        except Exception:
            result.append(
                repr(word)
            )

    return " ".join(result)


# ============================================================================
# RouterOS API
# ============================================================================

class RouterOSAPI:

    def __init__(
        self,
        host,
        port,
        username,
        password,
        timeout=10
    ):
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.timeout = timeout
        self.sock = None

    def connect(self):
        logging.info(
            "Connecting to MikroTik API: %s:%d",
            self.host,
            self.port
        )

        self.sock = socket.create_connection(
            (
                self.host,
                self.port
            ),
            timeout=self.timeout
        )

        self.sock.settimeout(
            self.timeout
        )

        self.login()

        logging.info(
            "Connected to MikroTik API successfully"
        )

    def login(self):
        ros_send_sentence(
            self.sock,
            [
                "/login",
                f"=name={self.username}",
                f"=password={self.password}"
            ]
        )

        response = ros_read_sentence(
            self.sock
        )

        if not response:
            raise RuntimeError(
                "Empty RouterOS login response"
            )

        response_dict = ros_sentence_to_dict(
            response
        )

        if (
            b"!done" in response
            and "ret" not in response_dict
        ):
            return

        challenge = response_dict.get(
            "ret"
        )

        if challenge:
            try:
                challenge_bytes = bytes.fromhex(
                    challenge
                )
            except ValueError as exc:
                raise RuntimeError(
                    "Invalid RouterOS login challenge"
                ) from exc

            digest = hashlib.md5()

            digest.update(
                b"\x00"
            )

            digest.update(
                self.password.encode(
                    "utf-8"
                )
            )

            digest.update(
                challenge_bytes
            )

            response_value = (
                "00"
                + digest.hexdigest()
            )

            ros_send_sentence(
                self.sock,
                [
                    "/login",
                    f"=name={self.username}",
                    f"=response={response_value}"
                ]
            )

            response = ros_read_sentence(
                self.sock
            )

            if b"!done" in response:
                return

            raise RuntimeError(
                "RouterOS challenge-response login failed: "
                + ros_response_to_text(response)
            )

        raise RuntimeError(
            "RouterOS API login failed: "
            + ros_response_to_text(response)
        )

    def command(self, words):
        ros_send_sentence(
            self.sock,
            words
        )

        responses = []

        while True:
            response = ros_read_sentence(
                self.sock
            )

            if not response:
                continue

            responses.append(
                response
            )

            if response[0] == b"!done":
                break

            if response[0] in (
                b"!trap",
                b"!fatal"
            ):
                break

        return responses

    def get_active_pppoe_users(self):
        responses = self.command([
            "/ppp/active/print",
            "=.proplist=name,service,session-id,address,caller-id"
        ])

        users = []

        for response in responses:
            if not response:
                continue

            if response[0] != b"!re":
                continue

            item = ros_sentence_to_dict(
                response[1:]
            )

            if item.get(
                "service",
                ""
            ).lower() != "pppoe":
                continue

            username = item.get(
                "name",
                ""
            )

            if not username:
                continue

            users.append({
                "username": username,
                "session_id": item.get(
                    "session-id",
                    ""
                ),
                "client_ip": item.get(
                    "address",
                    ""
                ),
                "caller_id": item.get(
                    "caller-id",
                    ""
                )
            })

        return users

    def get_simple_queues(self):
        responses = self.command([
            "/queue/simple/print",
            "=.proplist=.id,name,target,limit-at,max-limit,burst-limit,burst-threshold,burst-time,priority"
        ])

        queues = []

        for response in responses:
            if not response:
                continue

            if response[0] != b"!re":
                continue

            queues.append(
                ros_sentence_to_dict(
                    response[1:]
                )
            )

        return queues

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass

            self.sock = None


# ============================================================================
# Rate parsing
# ============================================================================

RATE_PATTERN = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kKmMgG]?)\s*$"
)


def parse_rate_value(value):
    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    match = RATE_PATTERN.match(
        value
    )

    if not match:
        return None

    number = float(
        match.group(1)
    )

    suffix = match.group(2).lower()

    multiplier = {
        "": 1,
        "k": 1000,
        "m": 1000000,
        "g": 1000000000
    }[suffix]

    return int(
        number * multiplier
    )


def parse_rate_pair(value):
    if not value:
        return None

    parts = str(value).strip().split(
        "/",
        1
    )

    rx = parse_rate_value(
        parts[0]
    )

    if rx is None:
        return None

    if len(parts) == 1:
        return (
            rx,
            rx
        )

    tx = parse_rate_value(
        parts[1]
    )

    if tx is None:
        tx = rx

    return (
        rx,
        tx
    )


# ============================================================================
# Rate formatting
# ============================================================================

def format_rate(value):
    value = int(value)

    if value >= 1000000000:
        if value % 1000000000 == 0:
            return f"{value // 1000000000}G"

        return f"{value / 1000000000:.2f}G"

    if value >= 1000000:
        if value % 1000000 == 0:
            return f"{value // 1000000}M"

        return f"{value / 1000000:.2f}M"

    if value >= 1000:
        if value % 1000 == 0:
            return f"{value // 1000}k"

        return f"{value / 1000:.2f}k"

    return str(value)


def round_burst_rate_down(value):
    """
    Round a calculated burst rate down to a display-friendly unit.

    Up to 999M only is allowed for Burst.
    No G unit is generated for Burst.
    """

    value = int(value)

    if value <= 0:
        return 1

    if value < 1000:
        return value

    if value < 1000000:
        rounded = (
            value // 1000
        ) * 1000

    elif value < 1000000000:
        rounded = (
            value // 1000000
        ) * 1000000

    else:
        rounded = MAX_BURST_BPS

    if rounded > MAX_BURST_BPS:
        rounded = MAX_BURST_BPS

    if rounded < 1:
        rounded = 1

    return rounded


def calculate_burst_rate(
    shared_bps,
    guaranteed_bps
):
    # Start from the rounded shared allocation.
    burst = round_burst_rate_down(
        shared_bps
    )

    # Burst must not be lower than Guaranteed,
    # unless Guaranteed itself is above the absolute
    # 999M Burst ceiling.
    if guaranteed_bps > MAX_BURST_BPS:
        return MAX_BURST_BPS

    if burst < guaranteed_bps:
        burst = round_burst_rate_down(
            guaranteed_bps
        )

        if burst < guaranteed_bps:
            burst = (
                (guaranteed_bps + 999999)
                // 1000000
            ) * 1000000

    if burst > MAX_BURST_BPS:
        burst = MAX_BURST_BPS

    return burst


# ============================================================================
# Queue matching
# ============================================================================

def target_contains_ip(
    target,
    client_ip
):
    if not target or not client_ip:
        return False

    try:
        client = ipaddress.ip_address(
            client_ip
        )
    except ValueError:
        return False

    for item in str(target).split(","):
        item = item.strip()

        if not item:
            continue

        try:
            if "/" in item:
                network = ipaddress.ip_network(
                    item,
                    strict=False
                )

                if client in network:
                    return True

            else:
                address = ipaddress.ip_address(
                    item
                )

                if client == address:
                    return True

        except ValueError:
            continue

    return False


def find_queue_for_user(
    queues,
    user
):
    username = user["username"]

    expected_name = (
        f"<pppoe-{username}>"
    )

    for queue in queues:
        if queue.get(
            "name",
            ""
        ) == expected_name:
            return queue

    for queue in queues:
        if queue.get(
            "name",
            ""
        ) == username:
            return queue

    client_ip = user.get(
        "client_ip",
        ""
    )

    for queue in queues:
        if target_contains_ip(
            queue.get(
                "target",
                ""
            ),
            client_ip
        ):
            return queue

    return None


# ============================================================================
# Guaranteed speed
# ============================================================================

def get_guaranteed_rate(queue):
    if not queue:
        return None

    limit_at = parse_rate_pair(
        queue.get(
            "limit-at",
            ""
        )
    )

    if limit_at:
        if (
            limit_at[0] > 0
            or limit_at[1] > 0
        ):
            return limit_at

    max_limit = parse_rate_pair(
        queue.get(
            "max-limit",
            ""
        )
    )

    if max_limit:
        if (
            max_limit[0] > 0
            or max_limit[1] > 0
        ):
            return max_limit

    return None


# ============================================================================
# RADIUS / CoA
# ============================================================================

def radius_attribute(
    attribute_type,
    value
):
    if isinstance(value, str):
        value = value.encode(
            "utf-8"
        )

    return (
        struct.pack(
            "!BB",
            attribute_type,
            len(value) + 2
        )
        + value
    )


def radius_mikrotik_rate_limit(
    value
):
    value = value.encode(
        "utf-8"
    )

    vendor_data = (
        struct.pack(
            "!I",
            MIKROTIK_VENDOR_ID
        )
        + bytes([
            MIKROTIK_RATE_LIMIT
        ])
        + bytes([
            len(value) + 2
        ])
        + value
    )

    return radius_attribute(
        26,
        vendor_data
    )


def build_radius_packet(
    code,
    identifier,
    authenticator,
    attributes
):
    attributes_data = b"".join(
        attributes
    )

    length = 20 + len(
        attributes_data
    )

    return (
        struct.pack(
            "!BBH",
            code,
            identifier,
            length
        )
        + authenticator
        + attributes_data
    )


def calculate_request_authenticator(
    secret,
    identifier,
    attributes
):
    zero_authenticator = (
        b"\x00" * 16
    )

    packet = build_radius_packet(
        RADIUS_CODE_COA_REQUEST,
        identifier,
        zero_authenticator,
        attributes
    )

    return hashlib.md5(
        packet
        + secret
    ).digest()


def parse_radius_attributes(data):
    attributes = []

    offset = 0

    while offset + 2 <= len(data):
        attribute_type = data[offset]
        attribute_length = data[offset + 1]

        if attribute_length < 2:
            break

        end = offset + attribute_length

        if end > len(data):
            break

        attributes.append(
            (
                attribute_type,
                data[
                    offset + 2:end
                ]
            )
        )

        offset = end

    return attributes


def get_error_cause(attributes):
    for attribute_type, value in attributes:
        if (
            attribute_type == ATTR_ERROR_CAUSE
            and len(value) == 4
        ):
            return struct.unpack(
                "!I",
                value
            )[0]

    return None


def verify_response_authenticator(
    secret,
    packet,
    request_authenticator
):
    if len(packet) < 20:
        return False

    code = packet[0]
    identifier = packet[1]

    length = struct.unpack(
        "!H",
        packet[2:4]
    )[0]

    if length != len(packet):
        return False

    expected = hashlib.md5(
        bytes([
            code,
            identifier
        ])
        + struct.pack(
            "!H",
            length
        )
        + request_authenticator
        + packet[20:length]
        + secret
    ).digest()

    return (
        expected
        == packet[4:20]
    )


def send_coa(
    mikrotik_host,
    coa_port,
    secret,
    username,
    guaranteed_rx,
    guaranteed_tx,
    burst_rx,
    burst_tx,
    burst_time_seconds,
    timeout=5
):
    secret_bytes = secret.encode(
        "utf-8"
    )

    identifier = random.randint(
        0,
        255
    )

    guaranteed = (
        f"{format_rate(guaranteed_rx)}/"
        f"{format_rate(guaranteed_tx)}"
    )

    burst = (
        f"{format_rate(burst_rx)}/"
        f"{format_rate(burst_tx)}"
    )

    threshold = guaranteed

    burst_time = (
        f"{burst_time_seconds}/"
        f"{burst_time_seconds}"
    )

    priority = "8"

    rate_min = guaranteed

    rate_limit = " ".join([
        guaranteed,
        burst,
        threshold,
        burst_time,
        priority,
        rate_min
    ])

    attributes = [
        radius_attribute(
            ATTR_USER_NAME,
            username
        ),
        radius_mikrotik_rate_limit(
            rate_limit
        )
    ]

    request_authenticator = (
        calculate_request_authenticator(
            secret_bytes,
            identifier,
            attributes
        )
    )

    packet = build_radius_packet(
        RADIUS_CODE_COA_REQUEST,
        identifier,
        request_authenticator,
        attributes
    )

    logging.info(
        'CoA user=%s rate-limit="%s"',
        username,
        rate_limit
    )

    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM
    )

    sock.settimeout(
        timeout
    )

    try:
        sock.sendto(
            packet,
            (
                mikrotik_host,
                int(coa_port)
            )
        )

        response, address = sock.recvfrom(
            4096
        )

        if len(response) < 20:
            logging.error(
                "Invalid RADIUS response from %s",
                address
            )
            return False

        response_code = response[0]
        response_identifier = response[1]

        if response_identifier != identifier:
            logging.error(
                "RADIUS identifier mismatch for user=%s",
                username
            )
            return False

        if not verify_response_authenticator(
            secret_bytes,
            response,
            request_authenticator
        ):
            logging.error(
                "Invalid RADIUS response authenticator for user=%s",
                username
            )
            return False

        response_attributes = parse_radius_attributes(
            response[20:]
        )

        if response_code == RADIUS_CODE_COA_ACK:
            logging.info(
                "CoA-ACK received for user=%s",
                username
            )
            return True

        if response_code == RADIUS_CODE_COA_NAK:
            error_cause = get_error_cause(
                response_attributes
            )

            logging.error(
                "CoA-NAK received for user=%s Error-Cause=%s",
                username,
                error_cause
                if error_cause is not None
                else "unknown"
            )

            return False

        logging.error(
            "Unexpected RADIUS response code=%d for user=%s",
            response_code,
            username
        )

        return False

    except socket.timeout:
        logging.error(
            "CoA timeout for user=%s -> %s:%d",
            username,
            mikrotik_host,
            coa_port
        )
        return False

    except Exception as exc:
        logging.error(
            "CoA error for user=%s: %s",
            username,
            exc
        )
        return False

    finally:
        sock.close()


# ============================================================================
# Queue verification
# ============================================================================

def log_queue_after_coa(
    queues,
    user
):
    queue = find_queue_for_user(
        queues,
        user
    )

    if not queue:
        logging.warning(
            "Queue not found after CoA: user=%s",
            user["username"]
        )
        return

    logging.info(
        "Queue after CoA user=%s "
        "limit-at=%s max-limit=%s "
        "burst-limit=%s burst-threshold=%s "
        "burst-time=%s priority=%s",
        user["username"],
        queue.get(
            "limit-at",
            "-"
        ),
        queue.get(
            "max-limit",
            "-"
        ),
        queue.get(
            "burst-limit",
            "-"
        ),
        queue.get(
            "burst-threshold",
            "-"
        ),
        queue.get(
            "burst-time",
            "-"
        ),
        queue.get(
            "priority",
            "-"
        )
    )


# ============================================================================
# Main processing
# ============================================================================

def process(config):
    mikrotik = config[
        "mikrotik"
    ]

    radius = config[
        "radius"
    ]

    total_mbps = float(
        config.get(
            "total_bandwidth_mbps",
            10
        )
    )

    burst_time_seconds = get_burst_time(
        config
    )

    api = RouterOSAPI(
        host=mikrotik["host"],
        port=int(
            mikrotik.get(
                "api_port",
                8728
            )
        ),
        username=mikrotik["username"],
        password=mikrotik["password"],
        timeout=10
    )

    try:
        api.connect()

        users = api.get_active_pppoe_users()

        logging.info(
            "Active PPPoE users: %d",
            len(users)
        )

        if not users:
            logging.info(
                "No active PPPoE users"
            )
            return

        queues = api.get_simple_queues()

        logging.info(
            "Simple queues found: %d",
            len(queues)
        )

        # All internal calculations use bits per second.
        total_bps = int(
            total_mbps * 1_000_000
        )

        shared_burst_bps = (
            total_bps // len(users)
        )

        if shared_burst_bps < 1:
            shared_burst_bps = 1

        rounded_shared_burst = (
            round_burst_rate_down(
                shared_burst_bps
            )
        )

        logging.info(
            "Total bandwidth: %.1f Mbps | "
            "Raw shared burst: %.3f Mbps | "
            "Rounded shared burst: %s",
            total_mbps,
            shared_burst_bps / 1_000_000,
            format_rate(
                rounded_shared_burst
            )
        )

        for user in users:
            username = user["username"]

            queue = find_queue_for_user(
                queues,
                user
            )

            if queue is None:
                logging.error(
                    "Queue not found for user=%s IP=%s",
                    username,
                    user.get(
                        "client_ip",
                        "-"
                    )
                )
                continue

            guaranteed = get_guaranteed_rate(
                queue
            )

            if guaranteed is None:
                logging.error(
                    "Cannot determine guaranteed speed for user=%s queue=%s",
                    username,
                    queue.get(
                        "name",
                        "-"
                    )
                )
                continue

            guaranteed_rx = guaranteed[0]
            guaranteed_tx = guaranteed[1]

            burst_rx = calculate_burst_rate(
                shared_burst_bps,
                guaranteed_rx
            )

            burst_tx = calculate_burst_rate(
                shared_burst_bps,
                guaranteed_tx
            )

            logging.info(
                "User=%s Guaranteed=%s/%s Shared=%s Final-Burst=%s/%s",
                username,
                format_rate(
                    guaranteed_rx
                ),
                format_rate(
                    guaranteed_tx
                ),
                format_rate(
                    rounded_shared_burst
                ),
                format_rate(
                    burst_rx
                ),
                format_rate(
                    burst_tx
                )
            )

            send_coa(
                mikrotik_host=mikrotik["host"],
                coa_port=int(
                    radius.get(
                        "coa_port",
                        1700
                    )
                ),
                secret=radius["secret"],
                username=username,
                guaranteed_rx=guaranteed_rx,
                guaranteed_tx=guaranteed_tx,
                burst_rx=burst_rx,
                burst_tx=burst_tx,
                burst_time_seconds=burst_time_seconds
            )

        logging.info(
            "Verifying Queue values after CoA"
        )

        updated_queues = api.get_simple_queues()

        for user in users:
            log_queue_after_coa(
                updated_queues,
                user
            )

    except Exception as exc:
        logging.exception(
            "Bandwidth manager error: %s",
            exc
        )

    finally:
        api.close()


# ============================================================================
# Program entry point
# ============================================================================

def main():
    config = load_config()

    setup_logging(
        config
    )

    logging.info(
        "PPPoE bandwidth manager started"
    )

    process(
        config
    )

    logging.info(
        "PPPoE bandwidth manager finished"
    )


if __name__ == "__main__":
    main()
