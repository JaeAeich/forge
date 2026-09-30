{
  description = "forge: a pipeline that turns real bug fixes into verified coding tasks for AI agents";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let pkgs = import nixpkgs { inherit system; };
      in {
        # `nix develop` gives every tool needed to build, lint, test and run forge.
        # Docker and Ollama are expected on the host (they need system services).
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [ python312 uv nodejs_22 go git ffmpeg kubectl kubeconform ];
          env = {
            UV_PYTHON = "${pkgs.python312}/bin/python3.12";
            UV_PYTHON_DOWNLOADS = "never";
          };
          shellHook = ''
            uv sync --quiet
            (cd web && npm install --silent)
          '';
        };
      });
}
