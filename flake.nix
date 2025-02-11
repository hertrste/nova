{
  description = "Cloud Hypervisor driver for OpenStack Nova";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs?ref=nixos-24.11";
    pre-commit-hooks-nix = {
      url = "github:cachix/pre-commit-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    openstack-nix.url = "git+ssh://git@gitlab.vpn.cyberus-technology.de/cyberus/cloud/openstack-nix.git";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      pre-commit-hooks-nix,
      openstack-nix,
      ...
    }:
    flake-utils.lib.eachSystem [ "x86_64-linux" ] (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        pre-commit-hooks-run = pre-commit-hooks-nix.lib.${system}.run;
        nixosModules = openstack-nix.nixosModules.${system};
      in
      {
        formatter = pkgs.nixfmt-rfc-style;
        devShells.default = pkgs.mkShellNoCC {
          inherit (self.checks.${system}.pre-commit-check) shellHook;
          buildInputs = self.checks.${system}.pre-commit-check.enabledPackages;
        };

        checks = import ./nix/checks { inherit pkgs pre-commit-hooks-run; };

        tests = import ./nix/tests/default.nix {
          inherit pkgs nixosModules;
        };
      }
    )
    // {
      ci = import ./nix/lib/gitlab-ci.nix { input = { inherit (self) checks tests; }; };
    };
}
