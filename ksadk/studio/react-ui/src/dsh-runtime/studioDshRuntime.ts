import { Context, type Fiber } from "@deepseek-ai/cordis";
import {
  StudioContributionRegistry,
  type StudioDshContributionMap,
  type StudioDshSlotName,
} from "./studioContributions";

export interface StudioDshUiService {
  register<K extends StudioDshSlotName>(
    context: Context,
    slot: K,
    contribution: StudioDshContributionMap[K],
  ): () => void;
}

export interface StudioDshService {
  ui: StudioDshUiService;
}

export type StudioDshContext = Context & { studio: StudioDshService };

/** Native DSH client plugin shape. No KsADK-specific plugin manifest exists. */
export interface DshClientPlugin {
  apply(context: StudioDshContext): void | Promise<void>;
  inject?: readonly string[];
  name?: string;
}

export interface MountedDshPlugin {
  dispose(): Promise<void>;
}

export class StudioDshRuntime {
  readonly contributions = new StudioContributionRegistry();
  private readonly context = new Context();

  constructor() {
    const registry = this.contributions;
    const service: StudioDshService = {
      ui: {
        register<K extends StudioDshSlotName>(
          context: Context,
          slot: K,
          contribution: StudioDshContributionMap[K],
        ) {
          return context.effect(() => registry.register(slot, contribution));
        },
      },
    };
    this.context.provide("studio", service);
  }

  async mount(plugin: DshClientPlugin): Promise<MountedDshPlugin> {
    const runtime = (context: Context) => plugin.apply(context as StudioDshContext);
    Object.assign(runtime, {
      inject: [...new Set(["studio", ...(plugin.inject ?? [])])],
    });
    if (plugin.name) Object.defineProperty(runtime, "name", { configurable: true, value: plugin.name });
    const fiber: Fiber = await this.context.plugin(runtime);
    return { dispose: () => fiber.dispose() };
  }

  async dispose(): Promise<void> {
    await this.context.fiber.dispose();
  }
}

/** The desktop shell and installed DSH client plugins share one Cordis root. */
export const studioDshRuntime = new StudioDshRuntime();
