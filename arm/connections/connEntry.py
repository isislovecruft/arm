"""
Connection panel entries related to actual connections to or from the system
(ie, results seen by netstat, lsof, etc).
"""

import time
import curses

from arm.util import torTools, uiTools
from arm.connections import entries

from stem.util import conf, connection, enum, str_tools

# Connection Categories:
#   Inbound      Relay connection, coming to us.
#   Outbound     Relay connection, leaving us.
#   Exit         Outbound relay connection leaving the Tor network.
#   Hidden       Connections to a hidden service we're providing.
#   Socks        Socks connections for applications using Tor.
#   Circuit      Circuits our tor client has created.
#   Directory    Fetching tor consensus information.
#   Control      Tor controller (arm, vidalia, etc).

Category = enum.Enum("INBOUND", "OUTBOUND", "EXIT", "HIDDEN", "SOCKS", "CIRCUIT", "DIRECTORY", "CONTROL")

CATEGORY_COLOR = {
  Category.INBOUND: "green",
  Category.OUTBOUND: "blue",
  Category.EXIT: "red",
  Category.HIDDEN: "magenta",
  Category.SOCKS: "yellow",
  Category.CIRCUIT: "cyan",
  Category.DIRECTORY: "magenta",
  Category.CONTROL: "red",
}

# static data for listing format
# <src>  -->  <dst>  <etc><padding>

LABEL_FORMAT = "%s  -->  %s  %s%s"
LABEL_MIN_PADDING = 2  # min space between listing label and following data

# sort value for scrubbed ip addresses

SCRUBBED_IP_VAL = 255 ** 4

CONFIG = conf.config_dict("arm", {
  "features.connection.markInitialConnections": True,
  "features.connection.showIps": True,
  "features.connection.showExitPort": True,
  "features.connection.showColumn.fingerprint": True,
  "features.connection.showColumn.nickname": True,
  "features.connection.showColumn.destination": True,
  "features.connection.showColumn.expandedIp": True,
})


class Endpoint:
  """
  Collection of attributes associated with a connection endpoint. This is a
  thin wrapper for torUtil functions, making use of its caching for
  performance.
  """

  def __init__(self, address, port):
    self.address = address
    self.port = port

    # if true, we treat the port as an definitely not being an ORPort when
    # searching for matching fingerprints (otherwise we use it to possably
    # narrow results when unknown)

    self.is_not_or_port = True

    # if set then this overwrites fingerprint lookups

    self.fingerprint_overwrite = None

  def get_address(self):
    """
    Provides the IP address of the endpoint.
    """

    return self.address

  def get_port(self):
    """
    Provides the port of the endpoint.
    """

    return self.port

  def get_hostname(self, default = None):
    """
    Provides the hostname associated with the relay's address. This is a
    non-blocking call and returns None if the address either can't be resolved
    or hasn't been resolved yet.

    Arguments:
      default - return value if no hostname is available
    """

    # TODO: skipping all hostname resolution to be safe for now
    #try:
    #  myHostname = hostnames.resolve(self.address)
    #except:
    #  # either a ValueError or IOError depending on the source of the lookup failure
    #  myHostname = None
    #
    #if not myHostname: return default
    #else: return myHostname

    return default

  def get_locale(self, default=None):
    """
    Provides the two letter country code for the IP address' locale.

    Arguments:
      default - return value if no locale information is available
    """

    conn = torTools.get_conn()
    return conn.get_info("ip-to-country/%s" % self.address, default)

  def get_fingerprint(self):
    """
    Provides the fingerprint of the relay, returning "UNKNOWN" if it can't be
    determined.
    """

    if self.fingerprint_overwrite:
      return self.fingerprint_overwrite

    conn = torTools.get_conn()
    my_fingerprint = conn.get_relay_fingerprint(self.address)

    # If there were multiple matches and our port is likely the ORPort then
    # try again with that to narrow the results.

    if not my_fingerprint and not self.is_not_or_port:
      my_fingerprint = conn.get_relay_fingerprint(self.address, self.port)

    if my_fingerprint:
      return my_fingerprint
    else:
      return "UNKNOWN"

  def get_nickname(self):
    """
    Provides the nickname of the relay, retuning "UNKNOWN" if it can't be
    determined.
    """

    my_fingerprint = self.get_fingerprint()

    if my_fingerprint != "UNKNOWN":
      conn = torTools.get_conn()
      my_nickname = conn.get_relay_nickname(my_fingerprint)

      if my_nickname:
        return my_nickname
      else:
        return "UNKNOWN"
    else:
      return "UNKNOWN"


class ConnectionEntry(entries.ConnectionPanelEntry):
  """
  Represents a connection being made to or from this system. These only
  concern real connections so it includes the inbound, outbound, directory,
  application, and controller categories.
  """

  def __init__(self, local_address, local_port, remote_address, remote_port):
    entries.ConnectionPanelEntry.__init__(self)
    self.lines = [ConnectionLine(local_address, local_port, remote_address, remote_port)]

  def get_sort_value(self, attr, listing_type):
    """
    Provides the value of a single attribute used for sorting purposes.
    """

    connection_line = self.lines[0]

    if attr == entries.SortAttr.IP_ADDRESS:
      if connection_line.is_private():
        return SCRUBBED_IP_VAL  # orders at the end

      return connection_line.sort_address
    elif attr == entries.SortAttr.PORT:
      return connection_line.sort_port
    elif attr == entries.SortAttr.HOSTNAME:
      if connection_line.is_private():
        return ""

      return connection_line.foreign.get_hostname("")
    elif attr == entries.SortAttr.FINGERPRINT:
      return connection_line.foreign.get_fingerprint()
    elif attr == entries.SortAttr.NICKNAME:
      my_nickname = connection_line.foreign.get_nickname()

      if my_nickname == "UNKNOWN":
        return "z" * 20  # orders at the end
      else:
        return my_nickname.lower()
    elif attr == entries.SortAttr.CATEGORY:
      return Category.index_of(connection_line.get_type())
    elif attr == entries.SortAttr.UPTIME:
      return connection_line.start_time
    elif attr == entries.SortAttr.COUNTRY:
      if connection.is_private_address(self.lines[0].foreign.get_address()):
        return ""
      else:
        return connection_line.foreign.get_locale("")
    else:
      return entries.ConnectionPanelEntry.get_sort_value(self, attr, listing_type)


class ConnectionLine(entries.ConnectionPanelLine):
  """
  Display component of the ConnectionEntry.
  """

  def __init__(self, local_address, local_port, remote_address, remote_port, include_port=True, include_expanded_addresses=True):
    entries.ConnectionPanelLine.__init__(self)

    self.local = Endpoint(local_address, local_port)
    self.foreign = Endpoint(remote_address, remote_port)
    self.start_time = time.time()
    self.is_initial_connection = False

    # overwrite the local fingerprint with ours

    conn = torTools.get_conn()
    self.local.fingerprint_overwrite = conn.get_info("fingerprint", None)

    # True if the connection has matched the properties of a client/directory
    # connection every time we've checked. The criteria we check is...
    #   client    - first hop in an established circuit
    #   directory - matches an established single-hop circuit (probably a
    #               directory mirror)

    self._possible_client = True
    self._possible_directory = True

    # attributes for SOCKS, HIDDEN, and CONTROL connections

    self.application_name = None
    self.application_pid = None
    self.is_application_resolving = False

    my_or_port = conn.get_option("ORPort", None)
    my_dir_port = conn.get_option("DirPort", None)
    my_socks_port = conn.get_option("SocksPort", "9050")
    my_ctl_port = conn.get_option("ControlPort", None)
    my_hidden_service_ports = conn.get_hidden_service_ports()

    # the ORListenAddress can overwrite the ORPort

    listen_addr = conn.get_option("ORListenAddress", None)

    if listen_addr and ":" in listen_addr:
      my_or_port = listen_addr[listen_addr.find(":") + 1:]

    if local_port in (my_or_port, my_dir_port):
      self.base_type = Category.INBOUND
      self.local.is_not_or_port = False
    elif local_port == my_socks_port:
      self.base_type = Category.SOCKS
    elif remote_port in my_hidden_service_ports:
      self.base_type = Category.HIDDEN
    elif local_port == my_ctl_port:
      self.base_type = Category.CONTROL
    else:
      self.base_type = Category.OUTBOUND
      self.foreign.is_not_or_port = False

    self.cached_type = None

    # includes the port or expanded ip address field when displaying listing
    # information if true

    self.include_port = include_port
    self.include_expanded_addresses = include_expanded_addresses

    # cached immutable values used for sorting

    ip_value = 0

    for comp in self.foreign.get_address().split("."):
      ip_value *= 255
      ip_value += int(comp)

    self.sort_address = ip_value
    self.sort_port = int(self.foreign.get_port())

  def get_listing_entry(self, width, current_time, listing_type):
    """
    Provides the tuple list for this connection's listing. Lines are composed
    of the following components:
      <src>  -->  <dst>     <etc>     <uptime> (<type>)

    ListingType.IP_ADDRESS:
      src - <internal addr:port> --> <external addr:port>
      dst - <destination addr:port>
      etc - <fingerprint> <nickname>

    ListingType.HOSTNAME:
      src - localhost:<port>
      dst - <destination hostname:port>
      etc - <destination addr:port> <fingerprint> <nickname>

    ListingType.FINGERPRINT:
      src - localhost
      dst - <destination fingerprint>
      etc - <nickname> <destination addr:port>

    ListingType.NICKNAME:
      src - <source nickname>
      dst - <destination nickname>
      etc - <fingerprint> <destination addr:port>

    Arguments:
      width       - maximum length of the line
      current_time - unix timestamp for what the results should consider to be
                    the current time
      listing_type - primary attribute we're listing connections by
    """

    # fetch our (most likely cached) display entry for the listing

    my_listing = entries.ConnectionPanelLine.get_listing_entry(self, width, current_time, listing_type)

    # fill in the current uptime and return the results

    if CONFIG["features.connection.markInitialConnections"]:
      time_prefix = "+" if self.is_initial_connection else " "
    else:
      time_prefix = ""

    time_label = time_prefix + "%5s" % str_tools.get_time_label(current_time - self.start_time, 1)
    my_listing[2] = (time_label, my_listing[2][1])

    return my_listing

  def is_unresolved_application(self):
    """
    True if our display uses application information that hasn't yet been resolved.
    """

    return self.application_name is None and self.get_type() in (Category.SOCKS, Category.HIDDEN, Category.CONTROL)

  def _get_listing_entry(self, width, current_time, listing_type):
    entry_type = self.get_type()

    # Lines are split into the following components in reverse:
    # init gap - " "
    # content  - "<src>  -->  <dst>     <etc>     "
    # time     - "<uptime>"
    # preType  - " ("
    # category - "<type>"
    # postType - ")   "

    line_format = uiTools.get_color(CATEGORY_COLOR[entry_type])
    time_width = 6 if CONFIG["features.connection.markInitialConnections"] else 5

    draw_entry = [(" ", line_format),
                  (self._get_listing_content(width - (12 + time_width) - 1, listing_type), line_format),
                  (" " * time_width, line_format),
                  (" (", line_format),
                  (entry_type.upper(), line_format | curses.A_BOLD),
                  (")" + " " * (9 - len(entry_type)), line_format)]

    return draw_entry

  def _get_details(self, width):
    """
    Provides details on the connection, correlated against available consensus
    data.

    Arguments:
      width - available space to display in
    """

    detail_format = curses.A_BOLD | uiTools.get_color(CATEGORY_COLOR[self.get_type()])
    return [(line, detail_format) for line in self._get_detail_content(width)]

  def reset_display(self):
    entries.ConnectionPanelLine.reset_display(self)
    self.cached_type = None

  def is_private(self):
    """
    Returns true if the endpoint is private, possibly belonging to a client
    connection or exit traffic.
    """

    if not CONFIG["features.connection.showIps"]:
      return True

    # This is used to scrub private information from the interface. Relaying
    # etiquette (and wiretapping laws) say these are bad things to look at so
    # DON'T CHANGE THIS UNLESS YOU HAVE A DAMN GOOD REASON!

    my_type = self.get_type()

    if my_type == Category.INBOUND:
      # if we're a guard or bridge and the connection doesn't belong to a
      # known relay then it might be client traffic

      conn = torTools.get_conn()

      if "Guard" in conn.get_my_flags([]) or conn.get_option("BridgeRelay", None) == "1":
        all_matches = conn.get_relay_fingerprint(self.foreign.get_address(), get_all_matches = True)

        return all_matches == []
    elif my_type == Category.EXIT:
      # DNS connections exiting us aren't private (since they're hitting our
      # resolvers). Everything else, however, is.

      # TODO: Ideally this would also double check that it's a UDP connection
      # (since DNS is the only UDP connections Tor will relay), however this
      # will take a bit more work to propagate the information up from the
      # connection resolver.

      return self.foreign.get_port() != "53"

    # for everything else this isn't a concern

    return False

  def get_type(self):
    """
    Provides our best guess at the current type of the connection. This
    depends on consensus results, our current client circuits, etc. Results
    are cached until this entry's display is reset.
    """

    # caches both to simplify the calls and to keep the type consistent until
    # we want to reflect changes

    if not self.cached_type:
      if self.base_type == Category.OUTBOUND:
        # Currently the only non-static categories are OUTBOUND vs...
        # - EXIT since this depends on the current consensus
        # - CIRCUIT if this is likely to belong to our guard usage
        # - DIRECTORY if this is a single-hop circuit (directory mirror?)
        #
        # The exitability, circuits, and fingerprints are all cached by the
        # torTools util keeping this a quick lookup.

        conn = torTools.get_conn()
        destination_fingerprint = self.foreign.get_fingerprint()

        if destination_fingerprint == "UNKNOWN":
          # Not a known relay. This might be an exit connection.

          if conn.is_exiting_allowed(self.foreign.get_address(), self.foreign.get_port()):
            self.cached_type = Category.EXIT
        elif self._possible_client or self._possible_directory:
          # This belongs to a known relay. If we haven't eliminated ourselves as
          # a possible client or directory connection then check if it still
          # holds true.

          my_circuits = conn.get_circuits()

          if self._possible_client:
            # Checks that this belongs to the first hop in a circuit that's
            # either unestablished or longer than a single hop (ie, anything but
            # a built 1-hop connection since those are most likely a directory
            # mirror).

            for _, status, _, path in my_circuits:
              if path and path[0] == destination_fingerprint and (status != "BUILT" or len(path) > 1):
                self.cached_type = Category.CIRCUIT  # matched a probable guard connection

            # if we fell through, we can eliminate ourselves as a guard in the future
            if not self.cached_type:
              self._possible_client = False

          if self._possible_directory:
            # Checks if we match a built, single hop circuit.

            for _, status, _, path in my_circuits:
              if path and path[0] == destination_fingerprint and status == "BUILT" and len(path) == 1:
                self.cached_type = Category.DIRECTORY

            # if we fell through, eliminate ourselves as a directory connection
            if not self.cached_type:
              self._possible_directory = False

      if not self.cached_type:
        self.cached_type = self.base_type

    return self.cached_type

  def get_etc_content(self, width, listing_type):
    """
    Provides the optional content for the connection.

    Arguments:
      width       - maximum length of the line
      listing_type - primary attribute we're listing connections by
    """

    # for applications show the command/pid

    if self.get_type() in (Category.SOCKS, Category.HIDDEN, Category.CONTROL):
      display_label = ""

      if self.application_name:
        if self.application_pid:
          display_label = "%s (%s)" % (self.application_name, self.application_pid)
        else:
          display_label = self.application_name
      elif self.is_application_resolving:
        display_label = "resolving..."
      else:
        display_label = "UNKNOWN"

      if len(display_label) < width:
        return ("%%-%is" % width) % display_label
      else:
        return ""

    # for everything else display connection/consensus information

    destination_address = self.get_destination_label(26, include_locale = True)
    etc, used_space = "", 0

    if listing_type == entries.ListingType.IP_ADDRESS:
      if width > used_space + 42 and CONFIG["features.connection.showColumn.fingerprint"]:
        # show fingerprint (column width: 42 characters)

        etc += "%-40s  " % self.foreign.get_fingerprint()
        used_space += 42

      if width > used_space + 10 and CONFIG["features.connection.showColumn.nickname"]:
        # show nickname (column width: remainder)

        nickname_space = width - used_space
        nickname_label = uiTools.crop_str(self.foreign.get_nickname(), nickname_space, 0)
        etc += ("%%-%is  " % nickname_space) % nickname_label
        used_space += nickname_space + 2
    elif listing_type == entries.ListingType.HOSTNAME:
      if width > used_space + 28 and CONFIG["features.connection.showColumn.destination"]:
        # show destination ip/port/locale (column width: 28 characters)
        etc += "%-26s  " % destination_address
        used_space += 28

      if width > used_space + 42 and CONFIG["features.connection.showColumn.fingerprint"]:
        # show fingerprint (column width: 42 characters)
        etc += "%-40s  " % self.foreign.get_fingerprint()
        used_space += 42

      if width > used_space + 17 and CONFIG["features.connection.showColumn.nickname"]:
        # show nickname (column width: min 17 characters, uses half of the remainder)
        nickname_space = 15 + (width - (used_space + 17)) / 2
        nickname_label = uiTools.crop_str(self.foreign.get_nickname(), nickname_space, 0)
        etc += ("%%-%is  " % nickname_space) % nickname_label
        used_space += (nickname_space + 2)
    elif listing_type == entries.ListingType.FINGERPRINT:
      if width > used_space + 17:
        # show nickname (column width: min 17 characters, consumes any remaining space)

        nickname_space = width - used_space - 2

        # if there's room then also show a column with the destination
        # ip/port/locale (column width: 28 characters)

        is_locale_included = width > used_space + 45
        is_locale_included &= CONFIG["features.connection.showColumn.destination"]

        if is_locale_included:
          nickname_space -= 28

        if CONFIG["features.connection.showColumn.nickname"]:
          nickname_label = uiTools.crop_str(self.foreign.get_nickname(), nickname_space, 0)
          etc += ("%%-%is  " % nickname_space) % nickname_label
          used_space += nickname_space + 2

        if is_locale_included:
          etc += "%-26s  " % destination_address
          used_space += 28
    else:
      if width > used_space + 42 and CONFIG["features.connection.showColumn.fingerprint"]:
        # show fingerprint (column width: 42 characters)
        etc += "%-40s  " % self.foreign.get_fingerprint()
        used_space += 42

      if width > used_space + 28 and CONFIG["features.connection.showColumn.destination"]:
        # show destination ip/port/locale (column width: 28 characters)
        etc += "%-26s  " % destination_address
        used_space += 28

    return ("%%-%is" % width) % etc

  def _get_listing_content(self, width, listing_type):
    """
    Provides the source, destination, and extra info for our listing.

    Arguments:
      width       - maximum length of the line
      listing_type - primary attribute we're listing connections by
    """

    conn = torTools.get_conn()
    my_type = self.get_type()
    destination_address = self.get_destination_label(26, include_locale = True)

    # The required widths are the sum of the following:
    # - room for LABEL_FORMAT and LABEL_MIN_PADDING (11 characters)
    # - base data for the listing
    # - that extra field plus any previous

    used_space = len(LABEL_FORMAT % tuple([""] * 4)) + LABEL_MIN_PADDING
    local_port = ":%s" % self.local.get_port() if self.include_port else ""

    src, dst, etc = "", "", ""

    if listing_type == entries.ListingType.IP_ADDRESS:
      my_external_address = conn.get_info("address", self.local.get_address())
      address_differ = my_external_address != self.local.get_address()

      # Expanding doesn't make sense, if the connection isn't actually
      # going through Tor's external IP address. As there isn't a known
      # method for checking if it is, we're checking the type instead.
      #
      # This isn't entirely correct. It might be a better idea to check if
      # the source and destination addresses are both private, but that might
      # not be perfectly reliable either.

      is_expansion_type = not my_type in (Category.SOCKS, Category.HIDDEN, Category.CONTROL)

      if is_expansion_type:
        src_address = my_external_address + local_port
      else:
        src_address = self.local.get_address() + local_port

      if my_type in (Category.SOCKS, Category.CONTROL):
        # Like inbound connections these need their source and destination to
        # be swapped. However, this only applies when listing by IP or hostname
        # (their fingerprint and nickname are both for us). Reversing the
        # fields here to keep the same column alignments.

        src = "%-21s" % destination_address
        dst = "%-26s" % src_address
      else:
        src = "%-21s" % src_address  # ip:port = max of 21 characters
        dst = "%-26s" % destination_address  # ip:port (xx) = max of 26 characters

      used_space += len(src) + len(dst)  # base data requires 47 characters

      # Showing the fingerprint (which has the width of 42) has priority over
      # an expanded address field. Hence check if we either have space for
      # both or wouldn't be showing the fingerprint regardless.

      is_expanded_address_visible = width > used_space + 28

      if is_expanded_address_visible and CONFIG["features.connection.showColumn.fingerprint"]:
        is_expanded_address_visible = width < used_space + 42 or width > used_space + 70

      if address_differ and is_expansion_type and is_expanded_address_visible and self.include_expanded_addresses and CONFIG["features.connection.showColumn.expandedIp"]:
        # include the internal address in the src (extra 28 characters)

        internal_address = self.local.get_address() + local_port

        # If this is an inbound connection then reverse ordering so it's:
        # <foreign> --> <external> --> <internal>
        # when the src and dst are swapped later

        if my_type == Category.INBOUND:
          src = "%-21s  -->  %s" % (src, internal_address)
        else:
          src = "%-21s  -->  %s" % (internal_address, src)

        used_space += 28

      etc = self.get_etc_content(width - used_space, listing_type)
      used_space += len(etc)
    elif listing_type == entries.ListingType.HOSTNAME:
      # 15 characters for source, and a min of 40 reserved for the destination
      # TODO: when actually functional the src and dst need to be swapped for
      # SOCKS and CONTROL connections

      src = "localhost%-6s" % local_port
      used_space += len(src)
      min_hostname_space = 40

      etc = self.get_etc_content(width - used_space - min_hostname_space, listing_type)
      used_space += len(etc)

      hostname_space = width - used_space
      used_space = width  # prevents padding at the end

      if self.is_private():
        dst = ("%%-%is" % hostname_space) % "<scrubbed>"
      else:
        hostname = self.foreign.get_hostname(self.foreign.get_address())
        port_label = ":%-5s" % self.foreign.get_port() if self.include_port else ""

        # truncates long hostnames and sets dst to <hostname>:<port>

        hostname = uiTools.crop_str(hostname, hostname_space, 0)
        dst = ("%%-%is" % hostname_space) % (hostname + port_label)
    elif listing_type == entries.ListingType.FINGERPRINT:
      src = "localhost"

      if my_type == Category.CONTROL:
        dst = "localhost"
      else:
        dst = self.foreign.get_fingerprint()

      dst = "%-40s" % dst

      used_space += len(src) + len(dst)  # base data requires 49 characters

      etc = self.get_etc_content(width - used_space, listing_type)
      used_space += len(etc)
    else:
      # base data requires 50 min characters
      src = self.local.get_nickname()

      if my_type == Category.CONTROL:
        dst = self.local.get_nickname()
      else:
        dst = self.foreign.get_nickname()

      min_base_space = 50

      etc = self.get_etc_content(width - used_space - min_base_space, listing_type)
      used_space += len(etc)

      base_space = width - used_space
      used_space = width  # prevents padding at the end

      if len(src) + len(dst) > base_space:
        src = uiTools.crop_str(src, base_space / 3)
        dst = uiTools.crop_str(dst, base_space - len(src))

      # pads dst entry to its max space

      dst = ("%%-%is" % (base_space - len(src))) % dst

    if my_type == Category.INBOUND:
      src, dst = dst, src

    padding = " " * (width - used_space + LABEL_MIN_PADDING)

    return LABEL_FORMAT % (src, dst, etc, padding)

  def _get_detail_content(self, width):
    """
    Provides a list with detailed information for this connection.

    Arguments:
      width - max length of lines
    """

    lines = [""] * 7
    lines[0] = "address: %s" % self.get_destination_label(width - 11)
    lines[1] = "locale: %s" % ("??" if self.is_private() else self.foreign.get_locale("??"))

    # Remaining data concerns the consensus results, with three possible cases:
    # - if there's a single match then display its details
    # - if there's multiple potential relays then list all of the combinations
    #   of ORPorts / Fingerprints
    # - if no consensus data is available then say so (probably a client or
    #   exit connection)

    fingerprint = self.foreign.get_fingerprint()
    conn = torTools.get_conn()

    if fingerprint != "UNKNOWN":
      # single match - display information available about it

      ns_entry = conn.get_consensus_entry(fingerprint)
      desc_entry = conn.get_descriptor_entry(fingerprint)

      # append the fingerprint to the second line

      lines[1] = "%-13sfingerprint: %s" % (lines[1], fingerprint)

      if ns_entry:
        # example consensus entry:
        # r murble R8sCM1ar1sS2GulQYFVmvN95xsk RJr6q+wkTFG+ng5v2bdCbVVFfA4 2011-02-21 00:25:32 195.43.157.85 443 0
        # s Exit Fast Guard Named Running Stable Valid
        # w Bandwidth=2540
        # p accept 20-23,43,53,79-81,88,110,143,194,443

        ns_lines = ns_entry.split("\n")

        first_line_comp = ns_lines[0].split(" ")

        if len(first_line_comp) >= 9:
          _, nickname, _, _, published_date, published_time, _, or_port, dir_port = first_line_comp[:9]
        else:
          nickname, published_date, published_time, or_port, dir_port = "", "", "", "", ""

        flags = "unknown"

        if len(ns_lines) >= 2 and ns_lines[1].startswith("s "):
          flags = ns_lines[1][2:]

        exit_policy = conn.get_relay_exit_policy(fingerprint)

        if exit_policy:
          policy_label = exit_policy.summary()
        else:
          policy_label = "unknown"

        dir_port_label = "" if dir_port == "0" else "dirport: %s" % dir_port
        lines[2] = "nickname: %-25s orport: %-10s %s" % (nickname, or_port, dir_port_label)
        lines[3] = "published: %s %s" % (published_time, published_date)
        lines[4] = "flags: %s" % flags.replace(" ", ", ")
        lines[5] = "exit policy: %s" % policy_label

      if desc_entry:
        tor_version, platform, contact = "", "", ""

        for desc_line in desc_entry.split("\n"):
          if desc_line.startswith("platform"):
            # has the tor version and platform, ex:
            # platform Tor 0.2.1.29 (r318f470bc5f2ad43) on Linux x86_64

            tor_version = desc_line[13:desc_line.find(" ", 13)]
            platform = desc_line[desc_line.rfind(" on ") + 4:]
          elif desc_line.startswith("contact"):
            contact = desc_line[8:]

            # clears up some highly common obscuring

            for alias in (" at ", " AT "):
              contact = contact.replace(alias, "@")

            for alias in (" dot ", " DOT "):
              contact = contact.replace(alias, ".")

            break  # contact lines come after the platform

        lines[3] = "%-35s os: %-14s version: %s" % (lines[3], platform, tor_version)

        # contact information is an optional field

        if contact:
          lines[6] = "contact: %s" % contact
    else:
      all_matches = conn.get_relay_fingerprint(self.foreign.get_address(), get_all_matches = True)

      if all_matches:
        # multiple matches
        lines[2] = "Multiple matches, possible fingerprints are:"

        for i in range(len(all_matches)):
          is_last_line = i == 3

          relay_port, relay_fingerprint = all_matches[i]
          line_text = "%i. or port: %-5s fingerprint: %s" % (i, relay_port, relay_fingerprint)

          # if there's multiple lines remaining at the end then give a count

          remaining_relays = len(all_matches) - i

          if is_last_line and remaining_relays > 1:
            line_text = "... %i more" % remaining_relays

          lines[3 + i] = line_text

          if is_last_line:
            break
      else:
        # no consensus entry for this ip address
        lines[2] = "No consensus data found"

    # crops any lines that are too long

    for i in range(len(lines)):
      lines[i] = uiTools.crop_str(lines[i], width - 2)

    return lines

  def get_destination_label(self, max_length, include_locale = False, include_hostname = False):
    """
    Provides a short description of the destination. This is made up of two
    components, the base <ip addr>:<port> and an extra piece of information in
    parentheses. The IP address is scrubbed from private connections.

    Extra information is...
    - the port's purpose for exit connections
    - the locale and/or hostname if set to do so, the address isn't private,
      and isn't on the local network
    - nothing otherwise

    Arguments:
      max_length       - maximum length of the string returned
      include_locale   - possibly includes the locale
      include_hostname - possibly includes the hostname
    """

    # the port and port derived data can be hidden by config or without include_port

    include_port = self.include_port and (CONFIG["features.connection.showExitPort"] or self.get_type() != Category.EXIT)

    # destination of the connection

    address_label = "<scrubbed>" if self.is_private() else self.foreign.get_address()
    port_label = ":%s" % self.foreign.get_port() if include_port else ""
    destination_address = address_label + port_label

    # Only append the extra info if there's at least a couple characters of
    # space (this is what's needed for the country codes).

    if len(destination_address) + 5 <= max_length:
      space_available = max_length - len(destination_address) - 3

      if self.get_type() == Category.EXIT and include_port:
        purpose = connection.port_usage(self.foreign.get_port())

        if purpose:
          # BitTorrent is a common protocol to truncate, so just use "Torrent"
          # if there's not enough room.

          if len(purpose) > space_available and purpose == "BitTorrent":
            purpose = "Torrent"

          # crops with a hyphen if too long

          purpose = uiTools.crop_str(purpose, space_available, end_type = uiTools.Ending.HYPHEN)

          destination_address += " (%s)" % purpose
      elif not connection.is_private_address(self.foreign.get_address()):
        extra_info = []
        conn = torTools.get_conn()

        if include_locale and not conn.is_geoip_unavailable():
          foreign_locale = self.foreign.get_locale("??")
          extra_info.append(foreign_locale)
          space_available -= len(foreign_locale) + 2

        if include_hostname:
          destination_hostname = self.foreign.get_hostname()

          if destination_hostname:
            # determines the full space available, taking into account the ", "
            # dividers if there's multiple pieces of extra data

            max_hostname_space = space_available - 2 * len(extra_info)
            destination_hostname = uiTools.crop_str(destination_hostname, max_hostname_space)
            extra_info.append(destination_hostname)
            space_available -= len(destination_hostname)

        if extra_info:
          destination_address += " (%s)" % ", ".join(extra_info)

    return destination_address[:max_length]
