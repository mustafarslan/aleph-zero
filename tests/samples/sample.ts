import { Session, refresh as doRefresh } from "./session";
import type { Config } from "../config";
import * as path from "node:path";

export const MAX_RETRY = 6;
let cache: Map<string, string> = new Map();

/** Exchange a refresh token. */
export async function refreshToken(session: Session, force = false): Promise<string> {
  return session.post("/oauth/refresh");
}

export const handler = async (req: Request): Promise<Response> => {
  return new Response("ok");
};

export interface Store { get(key: string): string | undefined; size: number; }
export type Id = string | number;
export enum Color { Red, Green }

export abstract class TokenStore extends Base implements Store {
  private ttl = 3600;
  static instances = 0;
  constructor(private readonly dir: string) { super(); }
  /** Number of tokens. */
  get size(): number { return cache.size; }
  get(key: string): string | undefined { return cache.get(key); }
  abstract flush(): void;
}
