const { join } = require("node:path");
export const add = (a) => (b) => a + b;
var legacy = function () { return 1; };

export default class Shelf {
  static count = 0;
  #secret() { return 42; }
  *items() { yield 1; }
}
module.exports = require("./other");
