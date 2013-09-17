"""
Unit tests for arm's initialization module.
"""

import getopt
import unittest

from mock import Mock, patch

from arm.starter import (
  _load_settings,
  _get_args,
  _get_controller,
  _authenticate,
  ARGS,
)

import stem
import stem.connection
import stem.socket

_load_settings()

class TestArgumentParsing(unittest.TestCase):
  def test_that_we_get_default_values(self):
    args = _get_args([])

    for attr in ARGS:
      self.assertEqual(ARGS[attr], getattr(args, attr))

  def test_that_we_load_arguments(self):
    args = _get_args(['--interface', '10.0.0.25:80'])
    self.assertEqual('10.0.0.25', args.control_address)
    self.assertEqual(80, args.control_port)

    args = _get_args(['--interface', '80'])
    self.assertEqual(ARGS['control_address'], args.control_address)
    self.assertEqual(80, args.control_port)

    args = _get_args(['--socket', '/tmp/my_socket', '--config', '/tmp/my_config'])
    self.assertEqual('/tmp/my_socket', args.control_socket)
    self.assertEqual('/tmp/my_config', args.config)

    args = _get_args(['--debug', '--blind'])
    self.assertEqual(True, args.debug)
    self.assertEqual(True, args.blind)

    args = _get_args(['--event', 'D1'])
    self.assertEqual('D1', args.logged_events)

    args = _get_args(['--version'])
    self.assertEqual(True, args.print_version)

    args = _get_args(['--help'])
    self.assertEqual(True, args.print_help)

  def test_examples(self):
    args = _get_args(['-b', '-i', '1643'])
    self.assertEqual(True, args.blind)
    self.assertEqual(1643, args.control_port)

    args = _get_args(['-e', 'we', '-c', '/tmp/cfg'])
    self.assertEqual('we', args.logged_events)
    self.assertEqual('/tmp/cfg', args.config)

  def test_that_we_reject_unrecognized_arguments(self):
    self.assertRaises(getopt.GetoptError, _get_args, ['--blarg', 'stuff'])

  def test_that_we_reject_invalid_interfaces(self):
    invalid_inputs = (
      '',
      '    ',
      'blarg',
      '127.0.0.1',
      '127.0.0.1:',
      ':80',
      '400.0.0.1:80',
      '127.0.0.1:-5',
      '127.0.0.1:500000',
    )

    for invalid_input in invalid_inputs:
      self.assertRaises(ValueError, _get_args, ['--interface', invalid_input])

class TestGetController(unittest.TestCase):
  @patch('os.path.exists', Mock(return_value = True))
  @patch('stem.util.system.is_running')
  @patch('stem.control.Controller.from_socket_file', Mock(side_effect = stem.SocketError('failed')))
  @patch('stem.control.Controller.from_port', Mock(side_effect = stem.SocketError('failed')))
  def test_failue_with_the_default_endpoint(self, is_running_mock):
    is_running_mock.return_value = False
    self._assert_get_controller_fails_with([], "Unable to connect to tor. Are you sure it's running?")

    is_running_mock.return_value = True
    self._assert_get_controller_fails_with([], "Unable to connect to tor. Maybe it's running without a ControlPort?")

  @patch('os.path.exists')
  @patch('stem.util.system.is_running', Mock(return_value = True))
  @patch('stem.control.Controller.from_socket_file', Mock(side_effect = stem.SocketError('failed')))
  @patch('stem.control.Controller.from_port', Mock(side_effect = stem.SocketError('failed')))
  def test_failure_with_a_custom_endpoint(self, path_exists_mock):
    path_exists_mock.return_value = True
    self._assert_get_controller_fails_with(['--interface', '80'], "Unable to connect to 127.0.0.1:80: failed")
    self._assert_get_controller_fails_with(['--socket', '/tmp/my_socket'], "Unable to connect to '/tmp/my_socket': failed")

    path_exists_mock.return_value = False
    self._assert_get_controller_fails_with(['--interface', '80'], "Unable to connect to 127.0.0.1:80: failed")
    self._assert_get_controller_fails_with(['--socket', '/tmp/my_socket'], "The socket file you specified (/tmp/my_socket) doesn't exist")

  @patch('os.path.exists', Mock(return_value = False))
  @patch('stem.control.Controller.from_port')
  def test_getting_a_control_port(self, from_port_mock):
    from_port_mock.return_value = 'success'

    self.assertEqual('success', _get_controller(_get_args([])))
    from_port_mock.assert_called_once_with('127.0.0.1', 9051)
    from_port_mock.reset_mock()

    self.assertEqual('success', _get_controller(_get_args(['--interface', '255.0.0.10:80'])))
    from_port_mock.assert_called_once_with('255.0.0.10', 80)

  @patch('os.path.exists', Mock(return_value = True))
  @patch('stem.control.Controller.from_socket_file')
  def test_getting_a_control_socket(self, from_socket_file_mock):
    from_socket_file_mock.return_value = 'success'

    self.assertEqual('success', _get_controller(_get_args([])))
    from_socket_file_mock.assert_called_once_with('/var/run/tor/control')
    from_socket_file_mock.reset_mock()

    self.assertEqual('success', _get_controller(_get_args(['--socket', '/tmp/my_socket'])))
    from_socket_file_mock.assert_called_once_with('/tmp/my_socket')

  def _assert_get_controller_fails_with(self, args, msg):
    try:
      _get_controller(_get_args(args))
      self.fail()
    except ValueError, exc:
      self.assertEqual(msg, str(exc))

class TestAuthenticate(unittest.TestCase):
  @patch('arm.util.torTools.get_chroot')
  def test_success(self, get_chroot_mock):
    controller = Mock()

    get_chroot_mock.return_value = ''  # no chroot
    _authenticate(controller, None)
    controller.authenticate.assert_called_with(password = None, chroot_path = '')
    controller.authenticate.reset_mock()

    get_chroot_mock.return_value = '/my/chroot'
    _authenticate(controller, 's3krit!!!')
    controller.authenticate.assert_called_with(password = 's3krit!!!', chroot_path = '/my/chroot')

  @patch('arm.util.torTools.get_chroot', Mock(return_value = ''))
  @patch('getpass.getpass')
  def test_success_with_password_prompt(self, getpass_mock):
    controller = Mock()

    def authenticate_mock(password, **kwargs):
      if password is None:
        raise stem.connection.MissingPassword('no password')
      elif password == 'my_password':
        return None  # success
      else:
        raise ValueError("Unexpected authenticate_mock input: %s" % password)

    controller.authenticate.side_effect = authenticate_mock
    getpass_mock.return_value = 'my_password'

    _authenticate(controller, None)
    controller.authenticate.assert_any_call(password = None, chroot_path = '')
    controller.authenticate.assert_any_call(password = 'my_password', chroot_path = '')

  @patch('arm.util.torTools.get_chroot', Mock(return_value = ''))
  def test_failure(self):
    controller = Mock()

    controller.authenticate.side_effect = stem.connection.IncorrectSocketType('unable to connect to socket')
    controller.get_socket.return_value = stem.socket.ControlPort(connect = False)
    self._assert_authenticate_fails_with(controller, 'Please check in your torrc that 9051 is the ControlPort.')

    controller.get_socket.return_value = stem.socket.ControlSocketFile(connect = False)
    self._assert_authenticate_fails_with(controller, 'Are you sure the interface you specified belongs to')

    controller.authenticate.side_effect = stem.connection.UnrecognizedAuthMethods('unable to connect', ['telepathy'])
    self._assert_authenticate_fails_with(controller, 'Tor is using a type of authentication we do not recognize...\n\n  telepathy')

    controller.authenticate.side_effect = stem.connection.IncorrectPassword('password rejected')
    self._assert_authenticate_fails_with(controller, 'Incorrect password')

    controller.authenticate.side_effect = stem.connection.UnreadableCookieFile('permission denied', '/tmp/my_cookie', False)
    self._assert_authenticate_fails_with(controller, "We were unable to read tor's authentication cookie...\n\n  Path: /tmp/my_cookie\n  Issue: permission denied")

    controller.authenticate.side_effect = stem.connection.OpenAuthRejected('crazy failure')
    self._assert_authenticate_fails_with(controller, 'Unable to authenticate: crazy failure')

  def _assert_authenticate_fails_with(self, controller, msg):
    try:
      _get_controller(_authenticate(controller, None))
      self.fail()
    except ValueError, exc:
      if not msg in str(exc):
        self.fail("Expected...\n\n%s\n\n... which couldn't be found in...\n\n%s" % (msg, exc))