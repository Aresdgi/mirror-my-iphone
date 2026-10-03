cask "mirror-my-iphone" do
  version "0.2.0"
  sha256 "57a915a7b1e9b52fd72223e4d444e5009c3ff4e3d79df334c3be2af3efbdeb5f"

  url "https://github.com/bhuwanadhikari/Mirror-my-iPhone/releases/download/v#{version}/Mirror-my-iPhone-#{version}.zip"
  name "Mirror my iPhone"
  desc "Mirror and control an iPhone over USB"
  homepage "https://github.com/bhuwanadhikari/Mirror-my-iPhone"

  depends_on formula: "python@3.12"
  depends_on macos: :ventura

  app "Mirror my iPhone.app"
  binary "#{appdir}/Mirror my iPhone.app/Contents/Resources/bin/mirror-my-iphone"

  postflight_steps do
    # The app is ad-hoc signed, not notarized: without this, macOS refuses to open it
    run "/usr/bin/xattr", args: ["-dr", "com.apple.quarantine", "{{appdir}}/Mirror my iPhone.app"]
    # Python environment, kept with this version in the Caskroom (the app looks for it there).
    # Install steps run sandboxed with a temporary HOME, so it can't go in ~/Library.
    # If this fails (e.g. offline), the app sets one up on its first launch instead.
    run "{{appdir}}/Mirror my iPhone.app/Contents/Resources/bootstrap.sh",
        args:           ["{{staged_path}}/venv"],
        must_succeed:   false,
        print_stdout:   true,
        network_access: true
  end

  uninstall quit: "io.github.bhuwanadhikari.mirrormyiphone"

  zap trash: [
    "~/Library/Application Support/Mirror my iPhone",
    "~/Library/Logs/Mirror my iPhone",
    "~/Library/Saved Application State/io.github.bhuwanadhikari.mirrormyiphone.savedState",
  ]

  caveats <<~EOS
    Open Mirror my iPhone from Spotlight or Launchpad. On first launch it runs a
    doctor that checks your iPhone and lists anything that's missing. Run it
    from the terminal any time with:
      mirror-my-iphone doctor

    Click, double-click and swipe need Xcode (free in the App Store) and
    Developer Mode on the iPhone; the doctor walks you through both.
  EOS
end
