#!/usr/bin/env node
'use strict';

// Documentation-only dependencies are supplied by the caller, outside the app.
const fs = require('node:fs/promises');
const path = require('node:path');
const {createHash} = require('node:crypto');
const root = path.resolve(__dirname, '../..');
const names = ['system-topology', 'data-control-planes', 'task-attempt-states',
  'transcript-identity', 'scheduler-lifecycle', 'release-verification'];
const digest = bytes => createHash('sha256').update(bytes).digest('hex');

async function main() {
  const mermaidRoot = process.env.FLEET_MERMAID_MODULE;
  const playwrightRoot = process.env.FLEET_PLAYWRIGHT_MODULE;
  if (!mermaidRoot || !playwrightRoot) {
    throw new Error('Set FLEET_MERMAID_MODULE and FLEET_PLAYWRIGHT_MODULE to installed module directories');
  }
  const document = await fs.readFile(path.join(root, 'docs/visual-architecture.md'), 'utf8');
  const sources = [...document.matchAll(/```mermaid\n([\s\S]*?)\n```/g)].map(match => match[1]);
  if (sources.length !== names.length) throw new Error('Expected exactly six architecture diagrams');
  const mermaidVersion = JSON.parse(await fs.readFile(path.join(mermaidRoot, 'package.json'), 'utf8')).version;
  const playwrightVersion = require(path.join(playwrightRoot, 'package.json')).version;
  const browser = await require(playwrightRoot).chromium.launch({
    channel: process.env.FLEET_BROWSER_CHANNEL || 'chromium', headless: true,
  });
  try {
    const page = await browser.newPage({deviceScaleFactor: 2});
    await page.setContent('<!doctype html><html><body style="margin:0;background:white"></body></html>');
    await page.addScriptTag({path: path.join(mermaidRoot, 'dist/mermaid.min.js')});
    await page.evaluate(() => mermaid.initialize({
      startOnLoad: false, securityLevel: 'strict', theme: 'base',
      themeVariables: {fontFamily: 'Arial, sans-serif', fontSize: '16px',
        primaryColor: '#edf4ff', primaryTextColor: '#142c46', primaryBorderColor: '#527ba5',
        lineColor: '#52718d', secondaryColor: '#eaf6ef', tertiaryColor: '#f8fafc'},
      flowchart: {htmlLabels: false, useMaxWidth: false, nodeSpacing: 25, rankSpacing: 35, curve: 'linear'},
      sequence: {useMaxWidth: false, actorMargin: 30, messageMargin: 25},
    }));
    const output = path.join(root, 'docs/assets/architecture');
    await fs.mkdir(output, {recursive: true});
    const diagrams = [];
    for (const [index, source] of sources.entries()) {
      const rendered = await page.evaluate(async ({source, index}) => {
        const {svg} = await mermaid.render(`architecture-${index}`, source);
        document.body.innerHTML = svg;
        const element = document.querySelector('svg');
        const box = element.viewBox.baseVal;
        const width = Math.ceil(box.width);
        const height = Math.ceil(box.height);
        element.setAttribute('width', String(width));
        element.setAttribute('height', String(height));
        element.style.maxWidth = 'none';
        element.style.backgroundColor = 'white';
        return {svg: element.outerHTML, width, height};
      }, {source, index});
      if (rendered.width < 1 || rendered.height < 1 || rendered.width > 5000 || rendered.height > 5000) {
        throw new Error(`Unexpected canvas dimensions for ${names[index]}`);
      }
      await page.setViewportSize({width: rendered.width, height: rendered.height});
      await page.evaluate(() => document.fonts.ready);
      const svg = Buffer.from(rendered.svg + '\n', 'utf8');
      const png = await page.locator('svg').screenshot();
      await fs.writeFile(path.join(output, names[index] + '.svg'), svg);
      await fs.writeFile(path.join(output, names[index] + '.png'), png);
      diagrams.push({name: names[index], source_sha256: digest(source),
        svg_sha256: digest(svg), png_sha256: digest(png),
        svg_width: rendered.width, svg_height: rendered.height, png_scale: 2});
    }
    await fs.writeFile(path.join(output, 'render-manifest.json'), JSON.stringify({
      source: 'docs/visual-architecture.md', mermaid: mermaidVersion,
      playwright: playwrightVersion, browser: browser.version(), diagrams,
    }, null, 2) + '\n', 'utf8');
    console.log(JSON.stringify(diagrams.map(({name, svg_width, svg_height}) => ({name, svg_width, svg_height}))));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
