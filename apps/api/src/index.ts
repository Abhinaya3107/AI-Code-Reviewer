import express from "express";
import pino from "pino";
import { config } from "./config.js";
import { webhooksRouter } from "./routes/webhooks.js";

const log = pino();
const app = express();

// Webhook router must come before any JSON body parser: it needs the raw body.
app.use(webhooksRouter);
app.get("/health", (_req, res) => res.json({ ok: true }));

app.listen(config.PORT, () => log.info(`API listening on :${config.PORT}`));
