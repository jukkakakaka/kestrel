-- 0012_broker_capabilities.sql — 券商能买什么，比费率更早决定计划能不能执行
--
-- 同样 1 手 100 股的腾讯、10 万本金、2% 风险预算：能买碎股的券商可以只买
-- 17 股，不能买的券商一手就超预算，只能拒绝。费率表说不了这件事。
--
-- 两个方向必须分开：
--   odd_lot_buy   港股**买入**不足一手的碎股（卖出碎股人人可做，不需要开关）
--   fractional_us 美股**按金额**买入小数股（0.0001 股起）
--
-- 默认全部为 0：没核实过的券商能力不能假装有。内置套餐的已知能力由
-- fees/plans.py 在播种与升级时补齐，用户可以在成本页自己改。

ALTER TABLE broker_plan ADD COLUMN odd_lot_buy INTEGER NOT NULL DEFAULT 0;
ALTER TABLE broker_plan ADD COLUMN fractional_us INTEGER NOT NULL DEFAULT 0;

-- 美股碎股常见门槛：单笔最低金额。0 表示不限制。
ALTER TABLE broker_plan ADD COLUMN fractional_min REAL NOT NULL DEFAULT 0;
