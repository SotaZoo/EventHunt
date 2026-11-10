/** @type {import('@cucumber/cucumber').IConfiguration} */
module.exports = {
  default: {
    require: ['support/**/*.ts'],
    requireModule: ['ts-node/register'],
    paths: ['features/**/*.feature'],
    format: ['progress-bar'],
    forceExit: true,
  },
}
