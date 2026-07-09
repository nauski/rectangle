{
  description = "rectangle — draw a box on a wlroots screen, record it, get a GIF or mp4 on your clipboard";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs:
        let
          # Runtime CLI tools that rectangle shells out to.
          runtimeTools = with pkgs; [ slurp wf-recorder ffmpeg wl-clipboard libnotify ];

          rectangle = pkgs.python3Packages.buildPythonApplication {
            pname = "rectangle";
            version = "0.1.0";
            src = ./.;
            pyproject = true;
            build-system = [ pkgs.python3Packages.setuptools ];

            # PyGObject + GTK4 for the optional GUI panel.
            dependencies = [ pkgs.python3Packages.pygobject3 ];
            nativeBuildInputs = [ pkgs.gobject-introspection pkgs.wrapGAppsHook4 ];
            buildInputs = [ pkgs.gtk4 ];

            # Put the wlroots tools on PATH and don't double-wrap the GApps env.
            dontWrapGApps = true;
            makeWrapperArgs = [
              "--prefix PATH : ${pkgs.lib.makeBinPath runtimeTools}"
              "\${gappsWrapperArgs[@]}"
            ];

            meta = with pkgs.lib; {
              description = "Region screen recorder for wlroots: GIF/mp4 to clipboard or file";
              license = licenses.mit;
              platforms = platforms.linux;
              mainProgram = "rectangle";
            };
          };
        in
        {
          inherit rectangle;
          default = rectangle;
        });

      apps = forAll (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.system}.rectangle}/bin/rectangle";
        };
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            (python3.withPackages (ps: [ ps.pygobject3 ]))
            gobject-introspection
            gtk4
            slurp
            wf-recorder
            ffmpeg
            wl-clipboard
            libnotify
          ];
          # so PyGObject can find the GTK4 typelibs in the dev shell
          shellHook = ''
            export GI_TYPELIB_PATH=${pkgs.gtk4}/lib/girepository-1.0:${pkgs.gobject-introspection}/lib/girepository-1.0:$GI_TYPELIB_PATH
            echo "rectangle dev shell — run: python -m rectangle gui"
          '';
        };
      });
    };
}
