cask "iphone-mirror" do
  version "0.2.0"
  sha256 "b5b0a116459eb613d55308ec5516afa9f6781e2224c34f3134dc3370083dc3d0"

  url "https://github.com/bhuwanadhikari/iPhoneMirroring/releases/download/v#{version}/iPhoneMirror-#{version}.zip"
  name "iPhone Mirror"
  desc "Mirror and control an iPhone over USB"
  homepage "https://github.com/bhuwanadhikari/iPhoneMirroring"

  depends_on formula: "python@3.12"
  depends_on macos: :ventura

  app "iPhone Mirror.app"
  binary "#{appdir}/iPhone Mirror.app/Contents/Resources/bin/iphone-mirror"

  postflight_steps do
    # The app is ad-hoc signed, not notarized: without this, macOS refuses to open it
    run "/usr/bin/xattr", args: ["-dr", "com.apple.quarantine", "{{appdir}}/iPhone Mirror.app"]
    # Python environment, kept with this version in the Caskroom (the app looks for it there).
    # Install steps run sandboxed with a temporary HOME, so it can't go in ~/Library.
    # If this fails (e.g. offline), the app sets one up on its first launch instead.
    run "{{appdir}}/iPhone Mirror.app/Contents/Resources/bootstrap.sh",
        args:           ["{{staged_path}}/venv"],
        must_succeed:   false,
        print_stdout:   true,
        network_access: true
  end

  uninstall quit: "io.github.bhuwanadhikari.iphonemirror"

  zap trash: [
    "~/Library/Application Support/iPhone Mirror",
    "~/Library/Logs/iPhone Mirror",
    "~/Library/Saved Application State/io.github.bhuwanadhikari.iphonemirror.savedState",
  ]

  caveats <<~EOS
    Open iPhone Mirror from Spotlight or Launchpad. On first launch it runs a
    doctor that checks your iPhone and lists anything that's missing. Run it
    from the terminal any time with:
      iphone-mirror doctor

    Click, double-click and swipe need Xcode (free in the App Store) and
    Developer Mode on the iPhone; the doctor walks you through both.
  EOS
end
