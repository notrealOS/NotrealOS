// SPDX-License-Identifier: GPL-2.0-only
/*
 * Awinic AW21018 18-channel LED driver (Nothing Phone (1) Glyph interface)
 *
 * Register layout and init sequence from Nothing's downstream leds_aw210xx.c.
 */

#include <linux/bits.h>
#include <linux/delay.h>
#include <linux/gpio/consumer.h>
#include <linux/i2c.h>
#include <linux/leds.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>

#define AW21018_REG_GCR		0x20
#define AW21018_REG_BR_L(ch)	(0x21 + 2 * (ch))
#define AW21018_REG_BR_H(ch)	(0x22 + 2 * (ch))
#define AW21018_REG_UPDATE	0x45
#define AW21018_REG_SL(ch)	(0x46 + (ch))
#define AW21018_REG_GCCR	0x58
#define AW21018_REG_UVCR	0x60
#define AW21018_REG_RESET	0x70	/* reads back the chip ID */

#define AW21018_CHIP_ID		0x02
#define AW21018_NUM_CH		18
#define AW21018_MAX_BRIGHTNESS	4095

#define AW21018_GCR_CHIPEN	BIT(0)
#define AW21018_GCR_PWMRES_12BIT	(2 << 1)
#define AW21018_GCR_CLKFRQ_16MHZ	(0 << 4)

struct aw21018;

struct aw21018_led {
	struct led_classdev cdev;
	struct aw21018 *chip;
	u32 channel;
};

struct aw21018 {
	struct i2c_client *client;
	struct gpio_desc *enable;
	struct mutex lock;
	u8 global_current;
};

static int aw21018_write(struct aw21018 *chip, u8 reg, u8 val)
{
	return i2c_smbus_write_byte_data(chip->client, reg, val);
}

static int aw21018_brightness_set(struct led_classdev *cdev,
				  enum led_brightness brightness)
{
	struct aw21018_led *led = container_of(cdev, struct aw21018_led, cdev);
	struct aw21018 *chip = led->chip;
	int ret;

	mutex_lock(&chip->lock);
	ret = aw21018_write(chip, AW21018_REG_BR_L(led->channel), brightness & 0xff);
	if (!ret)
		ret = aw21018_write(chip, AW21018_REG_BR_H(led->channel),
				    (brightness >> 8) & 0x0f);
	/* Brightness and scaling registers only take effect on an update write */
	if (!ret)
		ret = aw21018_write(chip, AW21018_REG_UPDATE, 0);
	mutex_unlock(&chip->lock);

	return ret;
}

static int aw21018_chip_init(struct aw21018 *chip)
{
	int ret;

	ret = i2c_smbus_read_byte_data(chip->client, AW21018_REG_RESET);
	if (ret < 0)
		return ret;
	if (ret != AW21018_CHIP_ID)
		return dev_err_probe(&chip->client->dev, -ENODEV,
				     "unexpected chip id 0x%02x\n", ret);

	ret = aw21018_write(chip, AW21018_REG_RESET, 0);
	if (ret)
		return ret;
	usleep_range(1000, 2000);

	ret = aw21018_write(chip, AW21018_REG_GCR, AW21018_GCR_CHIPEN |
			    AW21018_GCR_PWMRES_12BIT | AW21018_GCR_CLKFRQ_16MHZ);
	if (ret)
		return ret;

	ret = aw21018_write(chip, AW21018_REG_GCCR, chip->global_current);
	if (ret)
		return ret;

	/* Under-voltage protection and power-down enabled */
	return aw21018_write(chip, AW21018_REG_UVCR, 0);
}

static int aw21018_probe(struct i2c_client *client)
{
	struct device *dev = &client->dev;
	struct aw21018 *chip;
	u32 val;
	int ret;

	chip = devm_kzalloc(dev, sizeof(*chip), GFP_KERNEL);
	if (!chip)
		return -ENOMEM;

	chip->client = client;
	ret = devm_mutex_init(dev, &chip->lock);
	if (ret)
		return ret;

	chip->global_current = 0x20;
	if (!device_property_read_u32(dev, "awinic,global-current", &val))
		chip->global_current = min(val, 0xffU);

	chip->enable = devm_gpiod_get_optional(dev, "enable", GPIOD_OUT_HIGH);
	if (IS_ERR(chip->enable))
		return dev_err_probe(dev, PTR_ERR(chip->enable),
				     "failed to get enable gpio\n");
	usleep_range(1000, 2000);

	ret = aw21018_chip_init(chip);
	if (ret)
		return dev_err_probe(dev, ret, "chip init failed\n");

	device_for_each_child_node_scoped(dev, child) {
		struct led_init_data init_data = {};
		struct aw21018_led *led;
		u32 scaling = 0xff;

		ret = fwnode_property_read_u32(child, "reg", &val);
		if (ret || val >= AW21018_NUM_CH) {
			dev_warn(dev, "skipping LED with invalid reg\n");
			continue;
		}
		fwnode_property_read_u32(child, "awinic,scaling", &scaling);

		led = devm_kzalloc(dev, sizeof(*led), GFP_KERNEL);
		if (!led)
			return -ENOMEM;

		led->chip = chip;
		led->channel = val;
		led->cdev.max_brightness = AW21018_MAX_BRIGHTNESS;
		led->cdev.brightness_set_blocking = aw21018_brightness_set;

		ret = aw21018_write(chip, AW21018_REG_SL(val), min(scaling, 0xffU));
		if (ret)
			return ret;

		/* The DT label ("glyph:<zone>") is used as the LED name */
		init_data.fwnode = child;
		ret = devm_led_classdev_register_ext(dev, &led->cdev, &init_data);
		if (ret)
			return dev_err_probe(dev, ret, "failed to register LED %u\n", val);
	}

	return aw21018_write(chip, AW21018_REG_UPDATE, 0);
}

static void aw21018_remove(struct i2c_client *client)
{
	/* All LEDs off before the chip loses power */
	i2c_smbus_write_byte_data(client, AW21018_REG_RESET, 0);
}

static const struct of_device_id aw21018_of_match[] = {
	{ .compatible = "awinic,aw21018" },
	{ }
};
MODULE_DEVICE_TABLE(of, aw21018_of_match);

static struct i2c_driver aw21018_driver = {
	.driver = {
		.name = "leds-aw21018",
		.of_match_table = aw21018_of_match,
	},
	.probe = aw21018_probe,
	.remove = aw21018_remove,
};
module_i2c_driver(aw21018_driver);

MODULE_DESCRIPTION("Awinic AW21018 LED driver");
MODULE_LICENSE("GPL");
