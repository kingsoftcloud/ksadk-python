import { defineI18n } from "fumadocs-core/i18n";

// Chinese is the default language (unsuffixed files: `page.mdx`).
// English pages use the `.en.mdx` suffix.
export const i18n = defineI18n({
  defaultLanguage: "cn",
  languages: ["cn", "en"],
  // Public documentation must not silently serve Chinese content on an
  // English URL. CI enforces complete page and navigation metadata pairs.
  fallbackLanguage: null,
});
