{
  pkgs,
  generateRootwrapConf,
  nixosModules,
  novaPkg,
}:
let
  tests = {
    nova-chv-driver = pkgs.callPackage ./nova-chv-driver.nix {
      inherit nixosModules novaPkg generateRootwrapConf;
    };
  };
in
pkgs.lib.mapAttrs (_: v: pkgs.lib.recursiveUpdate v { meta.tag = "nix-integration-test"; }) tests
