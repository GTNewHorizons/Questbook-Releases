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

Each scheduled check reads the GTNH modpack release marked **Latest** on
GitHub. If there is no Latest release, its tag ends in `-pre`, or it has
already been mirrored, the check skips publication. Otherwise the workflow
extracts the questbook from the attached modpack ZIP, preserving release-time
version stamping.

Manual preview releases can be built from an upstream branch, tag, or commit.
Their release notes identify the requested ref and resolved commit.
