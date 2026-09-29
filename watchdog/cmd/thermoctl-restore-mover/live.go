package main

import "os"

// Targets bundles the real, live paths this program reads (to re-check
// they are empty) and writes into (once validation passes) -- the Go
// counterpart of agent.restore.RestoreTargets, minus staging_dir/data_dir,
// which this program addresses separately (see main.go's flags).
type Targets struct {
	ThermoctlDBPath string
	Zigbee2mqttDir  string
}

// liveStoreIsEmpty re-implements, byte for byte, the same definition
// agent/restore.py::_operational_store_is_empty already uses for its own
// advisory check -- a **shared contract** between the two programs
// (docs/STATUS.md's P5.5c section documents this explicitly): thermoctl's
// database is empty if it is absent or zero bytes; Zigbee2MQTT's
// directory is empty if neither database.db nor coordinator_backup.json
// exists there yet. A change to this definition on either side without
// the other is a contract break, not a private refactor -- if this
// function's logic ever needs to change, agent/restore.py's own function
// (and its docstring's "shared contract" note) has to change with it.
//
// **This is the authoritative check** (CLAUDE.md security principle 5):
// unlike agent/restore.py's own copy, which only ever reads a read-only
// bind mount and can therefore only ever refuse early, this is the check
// that actually decides whether anything gets moved -- a compromised or
// buggy agent process cannot route around it by staging anyway, since it
// runs again here regardless of what the agent already concluded.
//
// A stat error other than "does not exist" (an unreadable live path, a
// permission problem, anything this program did not anticipate) is
// treated the same as "not empty" -- fail closed, never proceed on an
// live path this program could not actually verify.
func liveStoreIsEmpty(targets Targets) (bool, error) {
	empty, err := pathIsAbsentOrEmptyFile(targets.ThermoctlDBPath)
	if err != nil || !empty {
		return false, err
	}
	for _, name := range []string{"database.db", "coordinator_backup.json"} {
		present, err := pathExists(targets.Zigbee2mqttDir + "/" + name)
		if err != nil {
			return false, err
		}
		if present {
			return false, nil
		}
	}
	return true, nil
}

func pathIsAbsentOrEmptyFile(path string) (bool, error) {
	info, err := os.Stat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return true, nil
		}
		return false, err
	}
	return info.Size() == 0, nil
}

func pathExists(path string) (bool, error) {
	_, err := os.Stat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}
