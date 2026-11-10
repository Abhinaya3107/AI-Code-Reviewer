import { PrismaClient } from "@prisma/client";
import { config } from "../config.js";

/** null when DATABASE_URL is not configured: the worker then runs without storing anything. */
export const db: PrismaClient | null = config.DATABASE_URL ? new PrismaClient() : null;
