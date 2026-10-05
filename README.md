# GTNH Questbook Releases

Questbook downloads for [GT New Horizons](https://github.com/GTNewHorizons/GT-New-Horizons-Modpack).

This repository packages the GTNH questbook into standalone ZIP archives.

The questbook source is maintained in [`GTNewHorizons/GT-New-Horizons-Modpack`](https://github.com/GTNewHorizons/GT-New-Horizons-Modpack/tree/master/config/betterquesting).

## Installation

Download the attached `GTNH-Questbook-*.zip` from the desired release. Do not
download GitHub's automatically generated **Source Code** archives.

1. Extract the downloaded ZIP anywhere.
2. Delete the existing `<GTNH instance>/config/betterquesting` folder.
3. Copy the extracted `betterquesting` folder into `<GTNH instance>/config`.
4. In-game, run `/bq_admin default load` to load the new questbook.

Delete the old folder before copying in the new one so files removed from the
questbook do not remain behind.

Doing this will NOT reset any player quest progress.

## Releases

GTNH modpack releases are mirrored automatically from their published ZIP
artifacts. This preserves the questbook shipped with each version, including
release-time version stamping. Automatic mirroring follows new upstream
releases; it does not backfill older versions.

Manual preview releases can be built from an upstream branch, tag, or commit.
Their release notes identify the requested ref and resolved commit.
