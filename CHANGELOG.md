# Changelog

All notable changes to orxaq-ops will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Atomic checkpoint writes to prevent corruption during interruption (W2 Backlog #2)
- Three new test cases for checkpoint write atomicity
- Temp file cleanup in checkpoint write operation

### Changed
- `write_checkpoint()` now uses atomic write-then-rename pattern
- In-progress tasks are automatically reset to pending state on resume

### Fixed
- Checkpoint files no longer corrupted if process is interrupted during write
- Temporary checkpoint files are properly cleaned up even if write fails
