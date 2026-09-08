// Node's data-URL test loader has no package-resolution base. Keep this file
// based bridge so the protocol test exercises the same published entrypoint
// used by Studio rather than a local copy.
export * from "@kingsoftcloud/ksadk-web/conversation";
