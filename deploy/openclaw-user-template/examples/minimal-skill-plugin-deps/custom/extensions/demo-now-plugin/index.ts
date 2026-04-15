import dayjs from "dayjs";

export default {
  id: "demo-now",
  name: "Demo Now Plugin",
  description: "Registers a tiny runtime inspection tool.",
  register(api) {
    api.registerTool({
      name: "demo_now",
      description:
        "Return the current time plus a small runtime summary from the demo plugin.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          format: {
            type: "string",
            description:
              "Optional dayjs format string, for example YYYY-MM-DD HH:mm:ss.",
          },
        },
      },
      async execute(_id, params = {}) {
        const format =
          typeof params.format === "string" && params.format.trim()
            ? params.format.trim()
            : "YYYY-MM-DD HH:mm:ss";
        const payload = {
          now: dayjs().format(format),
          appMode: process.env.APP_MODE || "(unset)",
          nodeVersion: process.version,
          pluginVersion: "0.1.0",
        };

        return {
          content: [
            {
              type: "text",
              text: JSON.stringify(payload, null, 2),
            },
          ],
        };
      },
    });
  },
};
