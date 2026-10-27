# Polytopia macOS UI Scale

Polytopia's Steam version on macOS can have a tiny UI even at its largest
scale setting. This unofficial patcher increases the game's global UI scale
and creates a patched `Polytopia.app` for you to use.

## Requirements and supported version

- macOS, Python 3.9 or newer, and Apple's `codesign` tool. No third-party
  Python packages are needed.
- An unmodified Steam `Polytopia.app`, version **2.17.3.16375**. Other builds
  need a compatibility review; see [official game updates](#official-game-updates).

## What the patcher does

The patcher runs locally. It reads the original Polytopia app and, by default,
creates `patched/Polytopia.app` and a report in this project folder. It leaves
the original untouched. You can choose another output location; updating an
existing patched app saves a backup beside it.

It makes no internet requests, installs no packages, and does not access your
saves, preferences, or logs. It uses Python and Apple's `codesign` tool; when
updating, it also checks whether Polytopia is running. The report includes
local file paths, so review it before sharing.

The default **3×** patch was visually verified to work on **macOS 27.0.1**,
on an M1 Mac mini with a 1080p display. Multiplayer and mod compatibility
are untested.

## Get the patcher

On the GitHub page for this project, choose **Code > Download ZIP**, then
open the downloaded ZIP to extract the folder. This is the simplest option.
If you already use Git, you can get the same folder in Terminal with:

```sh
git clone https://github.com/JimTesty/polytopia-macos-ui-scale.git
cd polytopia-macos-ui-scale
```

If you downloaded the ZIP, open Terminal, type `cd ` (include the space),
then drag the extracted project folder (usually named
`polytopia-macos-ui-scale-main`) from Finder into the Terminal window and
press Return. The commands below are run from that folder.

## Create the patched app

Steam's default macOS library puts the game here:
`~/Library/Application Support/Steam/steamapps/common/The Battle of Polytopia/`.
If your app is there, run this command from the patcher folder:

```sh
python3 tools/prepare_scaled_app.py \
  --source-app "$HOME/Library/Application Support/Steam/steamapps/common/The Battle of Polytopia/Polytopia.app"
```

If the app is not at that location, open Polytopia's Steam page, choose
**Manage > Browse Local Files**. In Terminal, type
`python3 tools/prepare_scaled_app.py --source-app ` (include the space at
the end), drag `Polytopia.app` from the Finder window into Terminal, then
press Return. This enters the app's path without typing or escaping it.

This creates **`patched/Polytopia.app`**, leaving the original app unchanged.
It also creates `patched/Polytopia.patch.json`, a record of the patch needed
for later size adjustments. Keep that report beside the patched app if you
move it.

Optional arguments:

- `--multiplier 2.0`: choose 2× instead of the default 3×. The value replaces
  the global multiplier; it does not multiply a previous patch.
- `--output-app "/another/folder/Polytopia.app"`: choose a different output
  location. Existing apps are never overwritten by this command.
- `--check`: check whether the original app is supported without creating
  a patched app.

Quit any running Polytopia instance, then open `patched/Polytopia.app` in
Finder to try it. Check menus and a local game for readable controls,
clipping, and correct mouse interaction. Until you install the patched app
as described below, Steam's Play button still uses the original.
Both apps share the game's preferences and save locations.

## Use the patched app through Steam

1. Quit Polytopia and Steam.
2. Rename the original `Polytopia.app` to `Polytopia.original.app` in the
   game's installation folder. Keep it for rollback and as the source for
   later size adjustments.
3. Move `patched/Polytopia.app` and its `Polytopia.patch.json` report into
   the same installation folder.

Steam's Play button will now use the patched app. To undo this, quit the
game and Steam. In the game folder, rename `Polytopia.app` to
`Polytopia.patched.app` and rename `Polytopia.patch.json` to
`Polytopia.patched.patch.json`; then rename `Polytopia.original.app` back to
`Polytopia.app`. Both apps and the patch report stay together in the game
folder. Official updates or Steam's file verification may replace the patch;
use the updated original to make a new patched app.

## If you're using an AI assistant

If you're using AI to run the patcher or help adapt it to an updated game,
give it read access to the original app and write access to the output
folder. If access is blocked, grant those paths or approve the command using
your AI tool's permission controls. Alternatively, copy the original app
into an accessible working folder, or run the command yourself in Terminal.
You can move the patched app into place manually without giving the AI
write access to Steam's installation folder.

## Adjust the size again

Keep the unmodified original and the patched app's adjacent `.patch.json`
report. Quit Polytopia, then specify both apps and the new multiplier:

```sh
python3 tools/prepare_scaled_app.py \
  --source-app "/path/to/unmodified/Polytopia.app" \
  --update-app "/path/to/patched/Polytopia.app" --multiplier 2.0
```

This updates the existing patched app without duplicating it again. The
tool checks its report and saves the three affected files and previous
report under `patch-backups/` beside the app. If patching or signing fails,
it restores those files. Both apps must be from the same official build.

If the tool cannot check whether Polytopia is running, confirm it is closed
yourself before adding `--game-closed-confirmed`.

## Official game updates

The patcher uses a **build profile**: a small JSON file recording a supported
game version, the checksum of its original asset file, and the location and
expected value of its UI scale setting. The repository includes one reviewed
[profile for 2.17.3.16375](profiles/steam-2.17.3.16375.json), which is selected
automatically. A different checksum stops patching, including when the source
is already patched.

Only `Contents/Resources/Data/data.unity3d` is checksummed, not the whole app.
Extra files such as `.DS_Store` or separate mod files do not affect this check;
mods that change `data.unity3d` do.

After an official update, try the unmodified game first. If it still needs
the workaround and the patcher rejects the new build, a maintainer or AI
assistant can use the discovery tool to locate the scale setting and write
a **proposed profile for that build**:

```sh
python3 tools/discover_ui_scale.py \
  --source-app "/path/to/updated/original/Polytopia.app" \
  --output-profile "patched/new-build-profile.json"
```

This inspects the app without patching it. The generated profile needs
review before use: confirm that it identifies the intended UI scale field
and presets, and that the game's layout and scaling code remain compatible.
Discovery recognizes a particular serialized record layout; finding it alone
does not establish compatibility. The current writer supports UnityFS v8 /
Unity 6000.3.17f1; another Unity revision needs a writer review too.

Once reviewed, pass `--profile "patched/new-build-profile.json"` to the
patcher with the updated original as `--source-app`. Create a fresh patched
app for a new official build. Do not reuse an old field offset or remove
compatibility checks just to get past a rejection.

## Implementation and development

The patcher changes `UICanvasScaleData.globalUIScaleMultiplier` in
`Contents/Resources/Data/data.unity3d`, verifies that all other decompressed
asset data is unchanged, then ad-hoc signs and checks the patched app.
Signing also changes `Contents/MacOS/Polytopia` and
`Contents/_CodeSignature/CodeResources`: three changed app files in total.
Unexpected or malformed values are rejected rather than silently clamped.
Asset and signature checks do not verify appearance or interaction.

Run `python3 -m unittest discover -s tests` for the synthetic tests. They mock
signing and require no game files. Keep generated apps, reports, saves, and
game assets out of Git; reports include local paths. `patched/` is ignored.

The source tools are [MIT licensed](LICENSE). No game assets are included.
