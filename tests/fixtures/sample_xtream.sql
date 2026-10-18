-- Sample mysqldump-style fixture resembling an Xtream-UI panel (anonymised).
-- Values are fake. Used only to exercise the parser and exporters.

DROP TABLE IF EXISTS `users`;
CREATE TABLE `users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `username` varchar(255) NOT NULL,
  `password` varchar(255) NOT NULL,
  `email` varchar(255) DEFAULT NULL,
  `is_admin` tinyint(1) NOT NULL DEFAULT 0,
  `notes` text,
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `users` VALUES
(1,'admin','$2y$10$abcdefghijklmnopqrstuv','admin@example.com',1,'owner account'),
(2,'reseller_a','$2y$10$0000000000000000000000','a@example.com',0,'line with ''quotes'' and, commas'),
(3,'line_001','$2y$10$1111111111111111111111',NULL,0,'multi\nline\nnote');

DROP TABLE IF EXISTS `streams`;
CREATE TABLE `streams` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `stream_display_name` varchar(255) DEFAULT NULL,
  `stream_source` text,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `streams` (`id`,`stream_display_name`,`stream_source`) VALUES (10,'Channel One','http://example.test/1');

DROP TABLE IF EXISTS `bouquets`;
CREATE TABLE `bouquets` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `bouquet_name` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `bouquets` VALUES (1,'Starter'),(2,'Full');

DROP TABLE IF EXISTS `settings`;
CREATE TABLE `settings` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `setting_key` varchar(255) DEFAULT NULL,
  `setting_value` text,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `settings` VALUES (1,'panel_name','My Panel'),(2,'version','1.0');
