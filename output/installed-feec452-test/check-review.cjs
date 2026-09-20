const {chromium}=require('C:/Users/GALAXYTY/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const {pathToFileURL}=require('node:url');
(async()=>{
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
const page=await browser.newPage({viewport:{width:1680,height:1050},deviceScaleFactor:1});
const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.goto(pathToFileURL('D:/PDF翻译skill实施/output/installed-feec452-test/jobs/e898c5d57555-20260917T083119Z/review.html').href);
await page.locator('.review-item').first().waitFor();
await page.screenshot({path:'D:/PDF翻译skill实施/output/installed-feec452-test/review-check.png',fullPage:true});
console.log(JSON.stringify({errors,items:await page.locator('.review-item').count(),stats:await page.locator('#stats').innerText(),exportDisabled:await page.locator('#export-review').isDisabled(),imageLoaded:await page.locator('#page-thumbnail').evaluate(i=>i.complete&&i.naturalWidth>0)},null,2));
await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
