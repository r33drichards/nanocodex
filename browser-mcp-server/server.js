#!/usr/bin/env node

/**
 * Browser Automation MCP Server for nanocodex
 *
 * Generic headless-Chromium automation with composable operation pipelines,
 * ported from NanoClaw's browser-mcp-server. Differences from the original:
 * outputs (screenshots, PDFs) are written to a local directory instead of S3
 * — on the languages images that is /work/browser, readable from the js
 * sandbox via fs.readFile — and there is no external policy layer in front
 * of this server, so it is only wired into trusted per-thread configs.
 *
 * Spawned per thread by codex as a stdio MCP server (declared alongside the
 * js sandbox by the AG-UI bridge, see client/nanocodex_client/agui/sandbox.py).
 * Each tool call launches a fresh browser and closes it: no state leaks
 * between calls or threads.
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import puppeteer from 'puppeteer-core';
import fs from 'fs';
import path from 'path';
import crypto from 'crypto';

const CHROMIUM_PATH = process.env.CHROMIUM_PATH || '/usr/bin/chromium';
const OUTPUT_DIR = process.env.BROWSER_OUTPUT_DIR || '/work/browser';

const DEFAULT_WIDTH = 1280;
const DEFAULT_HEIGHT = 800;
const MAX_WIDTH = 3840;
const MAX_HEIGHT = 2160;
const MAX_WAIT_MS = 10000;

function generateKey(prefix, ext) {
  const date = new Date().toISOString().split('T')[0];
  const id = crypto.randomBytes(8).toString('hex');
  return `${prefix}/${date}/${id}.${ext}`;
}

function saveFile(buffer, key) {
  const localPath = path.join(OUTPUT_DIR, key);
  const dir = path.dirname(localPath);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
  fs.writeFileSync(localPath, buffer);
  return localPath;
}

class BrowserSession {
  constructor() {
    this.browser = null;
    this.page = null;
  }

  async init() {
    this.browser = await puppeteer.launch({
      executablePath: CHROMIUM_PATH,
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    });
    this.page = await this.browser.newPage();
    await this.page.setViewport({ width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT });
  }

  async close() {
    if (this.browser) {
      await this.browser.close();
      this.browser = null;
      this.page = null;
    }
  }

  async runOperation(operation) {
    const { type, params = {} } = operation;

    switch (type) {
      case 'setViewport': {
        const width = Math.min(Math.max(params.width || DEFAULT_WIDTH, 320), MAX_WIDTH);
        const height = Math.min(Math.max(params.height || DEFAULT_HEIGHT, 200), MAX_HEIGHT);
        await this.page.setViewport({ width, height });
        return { width, height };
      }

      case 'navigate': {
        await this.page.goto(params.url, { waitUntil: 'networkidle0' });
        return { url: params.url };
      }

      case 'setContent': {
        await this.page.setContent(params.html, { waitUntil: 'networkidle0' });
        return { loaded: true };
      }

      case 'wait': {
        const ms = Math.min(params.ms || 0, MAX_WAIT_MS);
        if (params.selector) {
          await this.page.waitForSelector(params.selector, { timeout: ms || 5000 });
          return { waited_for: params.selector };
        }
        await new Promise((resolve) => setTimeout(resolve, ms));
        return { waited_ms: ms };
      }

      case 'screenshot': {
        const screenshot = await this.page.screenshot({ type: 'png', fullPage: params.fullPage || false });
        const savedPath = saveFile(screenshot, generateKey('screenshots', 'png'));
        return { path: savedPath, size: screenshot.length };
      }

      case 'pdf': {
        const pdf = await this.page.pdf({ format: params.format || 'A4', printBackground: true });
        const savedPath = saveFile(pdf, generateKey('pdfs', 'pdf'));
        return { path: savedPath, size: pdf.length };
      }

      case 'evaluate': {
        const result = await this.page.evaluate(params.script);
        return { result };
      }

      case 'click': {
        await this.page.click(params.selector);
        return { clicked: params.selector };
      }

      case 'type': {
        await this.page.type(params.selector, params.text);
        return { typed: params.text.length + ' chars', selector: params.selector };
      }

      case 'select': {
        const values = await this.page.select(params.selector, ...params.values);
        return { selected: values };
      }

      default:
        throw new Error(`Unknown operation: ${type}`);
    }
  }

  async executePipeline(operations) {
    const results = [];
    for (const op of operations) {
      try {
        const result = await this.runOperation(op);
        results.push({ success: true, result, operation: op.type });
      } catch (err) {
        results.push({ success: false, error: err.message || String(err), operation: op.type });
        break;
      }
    }
    return results;
  }
}

const server = new Server(
  { name: 'browser', version: '1.0.0' },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'browser_execute',
      description:
        'Execute browser automation operations as a composable pipeline.\n\n' +
        'Operations:\n' +
        '- setViewport: { width, height }\n' +
        '- navigate: { url } — go to URL\n' +
        '- setContent: { html } — load inline HTML\n' +
        '- wait: { ms } or { selector } — wait for time or element\n' +
        `- screenshot: { fullPage? } — capture PNG, saved under ${OUTPUT_DIR}\n` +
        `- pdf: { format? } — generate PDF, saved under ${OUTPUT_DIR}\n` +
        '- evaluate: { script } — run JS, returns result\n' +
        '- click: { selector }\n' +
        '- type: { selector, text }\n' +
        '- select: { selector, values }\n\n' +
        'Pipeline stops on first failure.\n\n' +
        'Examples:\n' +
        '- Screenshot: [{"type":"setContent","params":{"html":"<h1>Hi</h1>"}},{"type":"screenshot"}]\n' +
        '- With wait: [{"type":"setContent","params":{"html":"..."}},{"type":"wait","params":{"ms":2000}},{"type":"screenshot"}]\n' +
        '- Scrape: [{"type":"navigate","params":{"url":"https://example.com"}},{"type":"evaluate","params":{"script":"document.title"}}]',
      inputSchema: {
        type: 'object',
        properties: {
          operations: {
            type: 'array',
            description: 'Operations to execute in sequence',
            items: {
              type: 'object',
              properties: {
                type: {
                  type: 'string',
                  enum: ['setViewport', 'navigate', 'setContent', 'wait', 'screenshot', 'pdf', 'evaluate', 'click', 'type', 'select'],
                },
                params: {
                  type: 'object',
                  description: 'Operation-specific parameters',
                },
              },
              required: ['type'],
            },
          },
        },
        required: ['operations'],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  if (name !== 'browser_execute') {
    return { content: [{ type: 'text', text: `Unknown tool: ${name}` }], isError: true };
  }

  if (!args.operations || !Array.isArray(args.operations)) {
    return { content: [{ type: 'text', text: 'Error: operations array is required' }], isError: true };
  }

  const session = new BrowserSession();
  try {
    await session.init();
    const results = await session.executePipeline(args.operations);

    const failed = results.find((r) => !r.success);
    if (failed) {
      return {
        content: [{
          type: 'text',
          text: `Pipeline failed at "${failed.operation}": ${failed.error}\n\nCompleted:\n${JSON.stringify(results.slice(0, -1), null, 2)}`,
        }],
        isError: true,
      };
    }

    const screenshotResult = results.find((r) => r.operation === 'screenshot');
    const pdfResult = results.find((r) => r.operation === 'pdf');

    let message = `Pipeline completed (${results.length} operations)\n\nResults:\n${JSON.stringify(results, null, 2)}`;
    if (screenshotResult) {
      message += `\n\nScreenshot: ${screenshotResult.result.path}`;
    }
    if (pdfResult) {
      message += `\n\nPDF: ${pdfResult.result.path}`;
    }

    return { content: [{ type: 'text', text: message }] };
  } catch (err) {
    return { content: [{ type: 'text', text: `Error: ${err.message || String(err)}` }], isError: true };
  } finally {
    await session.close();
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
