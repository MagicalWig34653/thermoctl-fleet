#!/bin/bash -e
# pi-gen's own convention for every stage directory (confirmed against
# stage0/prerun.sh, stage1/prerun.sh, stage2/prerun.sh upstream): without
# this, pi-gen's `run_stage` never populates this stage's own
# `${ROOTFS_DIR}` (work/<image>/stage-thermoctl/rootfs) from the previous
# stage's finished rootfs, which `01-thermoctl/00-run.sh` and, later,
# pi-gen's own export-image step (`du`, image sizing) both need to exist.
if [ ! -d "${ROOTFS_DIR}" ]; then
	copy_previous
fi
