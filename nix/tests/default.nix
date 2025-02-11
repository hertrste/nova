{
  pkgs,
  nixosModules,
}:
let
  tests = {
    nova-chv-driver = pkgs.callPackage ./nova-chv-driver.nix { inherit nixosModules; };
  };
in
pkgs.lib.mapAttrs (_: v: pkgs.lib.recursiveUpdate v { meta.tag = "nix-integration-test"; }) tests
