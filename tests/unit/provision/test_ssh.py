import os
import re
import shlex
from typing import Optional
from unittest.mock import MagicMock

import pytest

import tmt.log
from tmt.guest import (
    GuestSsh,
    GuestSshData,
    _socket_path_hash,
    _socket_path_trivial,
)
from tmt.steps.provision import Provision
from tmt.utils import CommandOutput, Path
from tmt.utils.environment import Environment

ANSIBLE_TEST_INVENTORY = Path('/tmp/inventory.yaml')
ANSIBLE_TEST_WORKTREE = Path('/tmp/plan')
TEST_RUN_WORKDIR = Path('/tmp/run')
ANSIBLE_TEST_PLAYBOOK = Path('playbook.yml')
TEST_GUESTS: tuple[tuple[str, str], ...] = (
    ('guest-one', '192.0.2.1'),
    ('guest-two', '192.0.2.2'),
)


def _make_test_guests(parent: Provision, logger: tmt.log.Logger) -> dict[str, GuestSsh]:
    """Create the two guests used by the multi-guest SSH regression tests."""
    return {
        name: GuestSsh(
            logger=logger,
            parent=parent,
            name=name,
            data=GuestSshData(primary_address=address),
        )
        for name, address in TEST_GUESTS
    }


@pytest.mark.parametrize(
    ('socket_dir', 'limit_size', 'expected'),
    [
        (
            Path('/tmp'),
            True,
            Path('/tmp/dummy-id.socket'),
        ),
        (
            Path('/very/log/socket/dir/which/is/almost/over/the/limit/but/just/and/soo'),
            True,
            None,
        ),
        (
            Path('/very/log/socket/dir/which/is/over/the/limit/by/itself/and/does/not/need'),
            True,
            None,
        ),
        (
            Path('/very/log/socket/dir/which/is/almost/over/the/limit/but/just/and/soo'),
            False,
            Path(
                '/very/log/socket/dir/which/is/almost/over/the/limit/but/just/and/soo/dummy-id.socket'
            ),
        ),
        (
            Path('/very/log/socket/dir/which/is/over/the/limit/by/itself/and/does/not/need'),
            False,
            Path(
                '/very/log/socket/dir/which/is/over/the/limit/by/itself/and/does/not/need/dummy-id.socket'
            ),
        ),
    ],
)
def test_socket_path_trivial(
    socket_dir: Path, limit_size: bool, expected: Optional[Path], root_logger: tmt.log.Logger
) -> None:
    actual = _socket_path_trivial(
        socket_dir=socket_dir, guest_id='dummy-id', limit_size=limit_size, logger=root_logger
    )

    if expected is None:
        assert actual is expected

    else:
        assert str(actual) == str(expected)


@pytest.mark.parametrize(
    ('socket_dir', 'limit_size', 'expected'),
    [
        (Path('/tmp'), True, r'/tmp/[a-z0-9]{4}.socket'),
        (
            Path('/very/log/socket/dir/which/is/almost/over/the/limit/but/just/and/soo'),
            True,
            r'/very/log/socket/dir/which/is/almost/over/the/limit/but/just/and/soo/[a-z0-9]{4}.socket',
        ),
        (
            Path('/very/log/socket/dir/which/is/over/the/limit/by/itself/and/does/not/need'),
            True,
            None,
        ),
        (
            Path('/very/log/socket/dir/which/is/almost/over/the/limit/but/just/and/soo'),
            False,
            r'/very/log/socket/dir/which/is/almost/over/the/limit/but/just/and/soo/[a-z0-9]{4}.socket',
        ),
        (
            Path('/very/log/socket/dir/which/is/over/the/limit/by/itself/and/does/not/need'),
            False,
            r'/very/log/socket/dir/which/is/over/the/limit/by/itself/and/does/not/need/[a-z0-9]{4}.socket',
        ),
    ],
)
def test_socket_path_hash(
    socket_dir: Path,
    limit_size: bool,
    expected: Optional[str],
    monkeypatch,
    root_logger: tmt.log.Logger,
) -> None:
    monkeypatch.setattr(os, 'open', MagicMock(name='<mock>os.open'))
    monkeypatch.setattr(os, 'close', MagicMock(name='<mock>os.close'))

    actual = _socket_path_hash(
        socket_dir=socket_dir, guest_id='dummy-id', limit_size=limit_size, logger=root_logger
    )

    if expected is None:
        assert actual is expected

    else:
        assert re.match(expected, str(actual))


def test_socket_path_hash_conflict(monkeypatch, root_logger: tmt.log.Logger) -> None:
    monkeypatch.setattr(os, 'close', MagicMock(name='<mock>os.close'))

    socket_dir = Path('/tmp')

    # First, "create" a socket for the first guest.
    monkeypatch.setattr(os, 'open', MagicMock(name='<mock>os.open'))

    actual1 = _socket_path_hash(socket_dir=socket_dir, guest_id='dummy-id', logger=root_logger)

    # Second, simulate "file exists" error to inject the conflict. We
    # did not create the file, just pretending via capturing `os.open`.
    def _raise_but_once(filepath: Path, **kwargs):
        if str(filepath).replace('.reservation', '') == str(actual1):
            raise FileExistsError

        return MagicMock()

    monkeypatch.setattr(os, 'open', MagicMock(name='<mock>os.open', side_effect=_raise_but_once))

    actual2 = _socket_path_hash(socket_dir=socket_dir, guest_id='dummy-id', logger=root_logger)

    assert re.match(r'/tmp/[a-z0-9]{4}.socket', str(actual1))
    assert re.match(r'/tmp/[a-z0-9]{5}.socket', str(actual2))


def test_ansible_does_not_share_ssh_socket_between_guests(
    monkeypatch, root_logger: tmt.log.Logger
) -> None:
    """Ansible must not receive a shared literal ControlPath for multiple guests."""
    mock_mkdir = MagicMock(name='<mock>Path.mkdir')
    monkeypatch.setattr(Path, 'mkdir', mock_mkdir)

    mock_plan = MagicMock(name='mock<plan>')
    mock_plan_provision = MagicMock(name='mock<plan.provision>')
    mock_plan_provision.ansible_inventory_path = ANSIBLE_TEST_INVENTORY
    mock_plan.provision = mock_plan_provision
    mock_plan.worktree = ANSIBLE_TEST_WORKTREE

    mock_provision = MagicMock(spec=Provision)
    mock_provision.plan = mock_plan
    mock_provision.run_workdir = TEST_RUN_WORKDIR

    mock_run_guest_command = MagicMock(
        name='<mock>_run_guest_command',
        return_value=CommandOutput(stdout='', stderr=None),
    )
    mock_sanitize_playbook_path = MagicMock(return_value=ANSIBLE_TEST_PLAYBOOK)
    mock_prepare_command_environment = MagicMock(return_value=Environment())

    monkeypatch.setattr(
        GuestSsh,
        '_sanitize_ansible_playbook_path',
        mock_sanitize_playbook_path,
    )
    monkeypatch.setattr(
        GuestSsh,
        '_prepare_ansible_command_environment',
        mock_prepare_command_environment,
    )
    monkeypatch.setattr(GuestSsh, '_run_guest_command', mock_run_guest_command)

    guests = _make_test_guests(mock_provision, root_logger)

    for guest in guests.values():
        guest._run_ansible(ANSIBLE_TEST_PLAYBOOK)

    assert mock_run_guest_command.call_count == len(guests)

    ansible_commands = [call.args[0].to_popen() for call in mock_run_guest_command.call_args_list]
    ansible_limits = [command[command.index('--limit') + 1] for command in ansible_commands]
    ansible_ssh_options = [
        shlex.split(command[command.index('--ssh-common-args') + 1])
        for command in ansible_commands
    ]

    assert ansible_limits == list(guests)
    assert all(
        not any(
            option.startswith('-S') or option.lower().startswith('-ocontrolpath=')
            for option in options
        )
        for options in ansible_ssh_options
    )
    assert all('-oControlMaster=auto' in options for options in ansible_ssh_options)
    assert all('-oControlPersist=60s' in options for options in ansible_ssh_options)
