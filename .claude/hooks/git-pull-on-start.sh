#!/bin/bash
cd /root/aurel2 || exit 0
git pull --ff-only origin main 2>/dev/null || true
