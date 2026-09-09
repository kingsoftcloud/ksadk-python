/**
 * Studio intentionally owns no Conversation v1 decoder or reducer.
 *
 * Hosted UI, embedded Studio, and independent frontends all consume the
 * versioned, headless implementation published by ksadk-web. Keeping this
 * relative facade avoids a disruptive internal import migration while making
 * the package boundary explicit and auditable.
 */
export * from "@kingsoftcloud/ksadk-web/conversation";
export type * from "@kingsoftcloud/ksadk-web/conversation";
