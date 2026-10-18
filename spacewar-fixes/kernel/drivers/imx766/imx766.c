// SPDX-License-Identifier: GPL-2.0-only
/*
 * A V4L2 driver for Sony IMX766 cameras (Nothing Phone (1) main camera).
 * Register settings taken from the vendor sensor module (abra_qtech_imx766_wide).
 *
 * Based on the Sony IMX800 driver by Luca Weiss <luca.weiss@fairphone.com>,
 * itself based on Sony imx412 camera driver
 * Copyright (C) 2021 Intel Corporation
 */
#include <linux/unaligned.h>

#include <linux/clk.h>
#include <linux/delay.h>
#include <linux/i2c.h>
#include <linux/module.h>
#include <linux/pm_runtime.h>
#include <linux/regulator/consumer.h>

#include <media/v4l2-ctrls.h>
#include <media/v4l2-fwnode.h>
#include <media/v4l2-subdev.h>

/* Streaming Mode */
#define IMX766_REG_MODE_SELECT	0x0100
#define IMX766_MODE_STANDBY	0x00
#define IMX766_MODE_STREAMING	0x01

/* Lines per frame */
#define IMX766_REG_LPFR		0x0340

/* Chip ID */
#define IMX766_REG_ID		0x0016
#define IMX766_ID		0x766

/* Exposure control */
#define IMX766_REG_EXPOSURE_CIT	0x0202
#define IMX766_EXPOSURE_MIN	8
#define IMX766_EXPOSURE_OFFSET	48
#define IMX766_EXPOSURE_STEP	1
#define IMX766_EXPOSURE_DEFAULT	0x0800

/* Analog gain control */
#define IMX766_REG_AGAIN	0x0204
#define IMX766_AGAIN_MIN	0
#define IMX766_AGAIN_MAX	16128	/* 64x: gain = 16384 / (16384 - code) */
#define IMX766_AGAIN_STEP	1
#define IMX766_AGAIN_DEFAULT	0

/* Group hold register */
#define IMX766_REG_HOLD		0x0104

/* Input clock rate */
#define IMX766_INCLK_RATE	19200000

/* CSI2 HW configuration */
#define IMX766_LINK_FREQ	494400000	/* 988.8 Msps per trio / 2 */
#define IMX766_LINK_FREQ_BIN4	154800000	/* 309.6 Msps per trio / 2 */
#define IMX766_NUM_DATA_LANES	3	/* C-PHY trios */

/* Image orientation: no mirror/flip (the vendor 0x03 gave a 180-degree image here) */
#define IMX766_REG_ORIENTATION	0x0101
#define IMX766_ORIENTATION	0x00

#define IMX766_REG_MIN		0x00
#define IMX766_REG_MAX		0xffff

/**
 * struct imx766_reg - imx766 sensor register
 * @address: Register address
 * @val: Register value
 */
struct imx766_reg {
	u16 address;
	u8 val;
};

/**
 * struct imx766_reg_list - imx766 sensor register list
 * @num_of_regs: Number of registers in the list
 * @regs: Pointer to register list
 */
struct imx766_reg_list {
	u32 num_of_regs;
	const struct imx766_reg *regs;
};

/**
 * struct imx766_mode - imx766 sensor mode structure
 * @width: Frame width
 * @height: Frame height
 * @code: Format code
 * @hblank: Horizontal blanking in lines
 * @vblank: Vertical blanking in lines
 * @vblank_min: Minimum vertical blanking in lines
 * @vblank_max: Maximum vertical blanking in lines
 * @pclk: Sensor pixel clock
 * @link_freq_idx: Link frequency index
 * @reg_list: Register list for sensor mode
 */
struct imx766_mode {
	u32 width;
	u32 height;
	u32 code;
	u32 hblank;
	u32 vblank;
	u32 vblank_min;
	u32 vblank_max;
	u64 pclk;
	u32 link_freq_idx;
	struct imx766_reg_list reg_list;
};

/* In vendor power-up order */
static const char * const imx766_supply_names[] = {
	"vcustom",	/* custom rail on GPIO 72, enabled first by the vendor */
	"vana",		/* 2.8V Analog Power */
	"vdig",		/* 1.1V Digital Power */
	"vio",		/* 1.8V Interface Power */
};

/**
 * struct imx766 - imx766 sensor device structure
 * @dev: Pointer to generic device
 * @client: Pointer to i2c client
 * @sd: V4L2 sub-device
 * @pad: Media pad. Only one pad supported
 * @reset_gpio: Sensor reset gpio
 * @inclk: Sensor input clock
 * @supplies: Regulator supplies
 * @ctrl_handler: V4L2 control handler
 * @link_freq_ctrl: Pointer to link frequency control
 * @pclk_ctrl: Pointer to pixel clock control
 * @hblank_ctrl: Pointer to horizontal blanking control
 * @vblank_ctrl: Pointer to vertical blanking control
 * @exp_ctrl: Pointer to exposure control
 * @again_ctrl: Pointer to analog gain control
 * @vblank: Vertical blanking in lines
 * @cur_mode: Pointer to current selected sensor mode
 * @mutex: Mutex for serializing sensor controls
 */
struct imx766 {
	struct device *dev;
	struct i2c_client *client;
	struct v4l2_subdev sd;
	struct media_pad pad;
	struct gpio_desc *reset_gpio;
	struct clk *inclk;
	struct regulator_bulk_data supplies[ARRAY_SIZE(imx766_supply_names)];
	struct v4l2_ctrl_handler ctrl_handler;
	struct v4l2_ctrl *link_freq_ctrl;
	struct v4l2_ctrl *pclk_ctrl;
	struct v4l2_ctrl *hblank_ctrl;
	struct v4l2_ctrl *vblank_ctrl;
	struct {
		struct v4l2_ctrl *exp_ctrl;
		struct v4l2_ctrl *again_ctrl;
	};
	u32 vblank;
	const struct imx766_mode *cur_mode;
	struct mutex mutex;
};

static const s64 link_freq[] = {
	IMX766_LINK_FREQ,
	IMX766_LINK_FREQ_BIN4,
};

/* Common init registers */
static const struct imx766_reg common_regs[] = {
	{ 0x0136, 0x13 },
	{ 0x0137, 0x33 },
	{ 0x33f0, 0x06 },
	{ 0x33f1, 0x08 },
	{ 0x0111, 0x03 },
	{ 0x33d3, 0x01 },
	{ 0x3892, 0x01 },
	{ 0x4c14, 0x00 },
	{ 0x4c15, 0x07 },
	{ 0x4c16, 0x00 },
	{ 0x4c17, 0x1b },
	{ 0x4c1a, 0x00 },
	{ 0x4c1b, 0x03 },
	{ 0x4c1c, 0x00 },
	{ 0x4c1d, 0x00 },
	{ 0x4c1e, 0x00 },
	{ 0x4c1f, 0x02 },
	{ 0x4c20, 0x00 },
	{ 0x4c21, 0x5f },
	{ 0x4c26, 0x00 },
	{ 0x4c27, 0x43 },
	{ 0x4c28, 0x00 },
	{ 0x4c29, 0x09 },
	{ 0x4c2a, 0x00 },
	{ 0x4c2b, 0x4a },
	{ 0x4c2c, 0x00 },
	{ 0x4c2d, 0x00 },
	{ 0x4c2e, 0x00 },
	{ 0x4c2f, 0x02 },
	{ 0x4c30, 0x00 },
	{ 0x4c31, 0xc6 },
	{ 0x4c3e, 0x00 },
	{ 0x4c3f, 0x55 },
	{ 0x4c52, 0x00 },
	{ 0x4c53, 0x97 },
	{ 0x4cb4, 0x00 },
	{ 0x4cb5, 0x55 },
	{ 0x4cc8, 0x00 },
	{ 0x4cc9, 0x97 },
	{ 0x4d04, 0x00 },
	{ 0x4d05, 0x4f },
	{ 0x4d74, 0x00 },
	{ 0x4d75, 0x55 },
	{ 0x4f06, 0x00 },
	{ 0x4f07, 0x5f },
	{ 0x4f48, 0x00 },
	{ 0x4f49, 0xc6 },
	{ 0x544a, 0xff },
	{ 0x544b, 0xff },
	{ 0x544e, 0x01 },
	{ 0x544f, 0xbd },
	{ 0x5452, 0xff },
	{ 0x5453, 0xff },
	{ 0x5456, 0x00 },
	{ 0x5457, 0xa5 },
	{ 0x545a, 0xff },
	{ 0x545b, 0xff },
	{ 0x545e, 0x00 },
	{ 0x545f, 0xa5 },
	{ 0x5496, 0x00 },
	{ 0x5497, 0xa2 },
	{ 0x54f6, 0x01 },
	{ 0x54f7, 0x55 },
	{ 0x54f8, 0x01 },
	{ 0x54f9, 0x61 },
	{ 0x5670, 0x00 },
	{ 0x5671, 0x85 },
	{ 0x5672, 0x01 },
	{ 0x5673, 0x77 },
	{ 0x5674, 0x01 },
	{ 0x5675, 0x2f },
	{ 0x5676, 0x02 },
	{ 0x5677, 0x55 },
	{ 0x5678, 0x00 },
	{ 0x5679, 0x85 },
	{ 0x567a, 0x01 },
	{ 0x567b, 0x77 },
	{ 0x567c, 0x01 },
	{ 0x567d, 0x2f },
	{ 0x567e, 0x02 },
	{ 0x567f, 0x55 },
	{ 0x5680, 0x00 },
	{ 0x5681, 0x85 },
	{ 0x5682, 0x01 },
	{ 0x5683, 0x77 },
	{ 0x5684, 0x01 },
	{ 0x5685, 0x2f },
	{ 0x5686, 0x02 },
	{ 0x5687, 0x55 },
	{ 0x5688, 0x00 },
	{ 0x5689, 0x85 },
	{ 0x568a, 0x01 },
	{ 0x568b, 0x77 },
	{ 0x568c, 0x01 },
	{ 0x568d, 0x2f },
	{ 0x568e, 0x02 },
	{ 0x568f, 0x55 },
	{ 0x5690, 0x01 },
	{ 0x5691, 0x7a },
	{ 0x5692, 0x02 },
	{ 0x5693, 0x6c },
	{ 0x5694, 0x01 },
	{ 0x5695, 0x35 },
	{ 0x5696, 0x02 },
	{ 0x5697, 0x5b },
	{ 0x5698, 0x01 },
	{ 0x5699, 0x7a },
	{ 0x569a, 0x02 },
	{ 0x569b, 0x6c },
	{ 0x569c, 0x01 },
	{ 0x569d, 0x35 },
	{ 0x569e, 0x02 },
	{ 0x569f, 0x5b },
	{ 0x56a0, 0x01 },
	{ 0x56a1, 0x7a },
	{ 0x56a2, 0x02 },
	{ 0x56a3, 0x6c },
	{ 0x56a4, 0x01 },
	{ 0x56a5, 0x35 },
	{ 0x56a6, 0x02 },
	{ 0x56a7, 0x5b },
	{ 0x56a8, 0x01 },
	{ 0x56a9, 0x80 },
	{ 0x56aa, 0x02 },
	{ 0x56ab, 0x72 },
	{ 0x56ac, 0x01 },
	{ 0x56ad, 0x2f },
	{ 0x56ae, 0x02 },
	{ 0x56af, 0x55 },
	{ 0x5902, 0x0e },
	{ 0x5a50, 0x04 },
	{ 0x5a51, 0x04 },
	{ 0x5a69, 0x01 },
	{ 0x5c49, 0x0d },
	{ 0x5d60, 0x08 },
	{ 0x5d61, 0x08 },
	{ 0x5d62, 0x08 },
	{ 0x5d63, 0x08 },
	{ 0x5d64, 0x08 },
	{ 0x5d67, 0x08 },
	{ 0x5d6c, 0x08 },
	{ 0x5d6e, 0x08 },
	{ 0x5d71, 0x08 },
	{ 0x5d8e, 0x14 },
	{ 0x5d90, 0x03 },
	{ 0x5d91, 0x0a },
	{ 0x5d92, 0x1f },
	{ 0x5d93, 0x05 },
	{ 0x5d97, 0x1f },
	{ 0x5d9a, 0x06 },
	{ 0x5d9c, 0x1f },
	{ 0x5da1, 0x1f },
	{ 0x5da6, 0x1f },
	{ 0x5da8, 0x1f },
	{ 0x5dab, 0x1f },
	{ 0x5dc0, 0x06 },
	{ 0x5dc1, 0x06 },
	{ 0x5dc2, 0x07 },
	{ 0x5dc3, 0x06 },
	{ 0x5dc4, 0x07 },
	{ 0x5dc7, 0x07 },
	{ 0x5dcc, 0x07 },
	{ 0x5dce, 0x07 },
	{ 0x5dd1, 0x07 },
	{ 0x5e3e, 0x00 },
	{ 0x5e3f, 0x00 },
	{ 0x5e41, 0x00 },
	{ 0x5e48, 0x00 },
	{ 0x5e49, 0x00 },
	{ 0x5e4a, 0x00 },
	{ 0x5e4c, 0x00 },
	{ 0x5e4d, 0x00 },
	{ 0x5e4e, 0x00 },
	{ 0x6026, 0x03 },
	{ 0x6028, 0x03 },
	{ 0x602a, 0x03 },
	{ 0x602c, 0x03 },
	{ 0x602f, 0x03 },
	{ 0x6036, 0x03 },
	{ 0x6038, 0x03 },
	{ 0x603a, 0x03 },
	{ 0x603c, 0x03 },
	{ 0x603f, 0x03 },
	{ 0x6074, 0x19 },
	{ 0x6076, 0x19 },
	{ 0x6078, 0x19 },
	{ 0x607a, 0x19 },
	{ 0x607d, 0x19 },
	{ 0x6084, 0x32 },
	{ 0x6086, 0x32 },
	{ 0x6088, 0x32 },
	{ 0x608a, 0x32 },
	{ 0x608d, 0x32 },
	{ 0x60c2, 0x4a },
	{ 0x60c4, 0x4a },
	{ 0x60cb, 0x4a },
	{ 0x60d2, 0x4a },
	{ 0x60d4, 0x4a },
	{ 0x60db, 0x4a },
	{ 0x62f9, 0x14 },
	{ 0x6305, 0x13 },
	{ 0x6307, 0x13 },
	{ 0x630a, 0x13 },
	{ 0x630d, 0x0d },
	{ 0x6317, 0x0d },
	{ 0x632f, 0x2e },
	{ 0x6333, 0x2e },
	{ 0x6339, 0x2e },
	{ 0x6343, 0x2e },
	{ 0x6347, 0x2e },
	{ 0x634d, 0x2e },
	{ 0x6352, 0x00 },
	{ 0x6353, 0x5f },
	{ 0x6366, 0x00 },
	{ 0x6367, 0x5f },
	{ 0x638f, 0x95 },
	{ 0x6393, 0x95 },
	{ 0x6399, 0x95 },
	{ 0x63a3, 0x95 },
	{ 0x63a7, 0x95 },
	{ 0x63ad, 0x95 },
	{ 0x63b2, 0x00 },
	{ 0x63b3, 0xc6 },
	{ 0x63c6, 0x00 },
	{ 0x63c7, 0xc6 },
	{ 0x8bdb, 0x02 },
	{ 0x8bde, 0x02 },
	{ 0x8be1, 0x2d },
	{ 0x8be4, 0x00 },
	{ 0x8be5, 0x00 },
	{ 0x8be6, 0x01 },
	{ 0x9002, 0x14 },
	{ 0x9200, 0xb5 },
	{ 0x9201, 0x9e },
	{ 0x9202, 0xb5 },
	{ 0x9203, 0x42 },
	{ 0x9204, 0xb5 },
	{ 0x9205, 0x43 },
	{ 0x9206, 0xbd },
	{ 0x9207, 0x20 },
	{ 0x9208, 0xbd },
	{ 0x9209, 0x22 },
	{ 0x920a, 0xbd },
	{ 0x920b, 0x23 },
	{ 0xb5d7, 0x10 },
	{ 0xbd24, 0x00 },
	{ 0xbd25, 0x00 },
	{ 0xbd26, 0x00 },
	{ 0xbd27, 0x00 },
	{ 0xbd28, 0x00 },
	{ 0xbd29, 0x00 },
	{ 0xbd2a, 0x00 },
	{ 0xbd2b, 0x00 },
	{ 0xbd2c, 0x32 },
	{ 0xbd2d, 0x70 },
	{ 0xbd2e, 0x25 },
	{ 0xbd2f, 0x30 },
	{ 0xbd30, 0x3b },
	{ 0xbd31, 0xe0 },
	{ 0xbd32, 0x69 },
	{ 0xbd33, 0x40 },
	{ 0xbd34, 0x25 },
	{ 0xbd35, 0x90 },
	{ 0xbd36, 0x58 },
	{ 0xbd37, 0x00 },
	{ 0xbd38, 0x00 },
	{ 0xbd39, 0x00 },
	{ 0xbd3a, 0x00 },
	{ 0xbd3b, 0x00 },
	{ 0xbd3c, 0x32 },
	{ 0xbd3d, 0x70 },
	{ 0xbd3e, 0x25 },
	{ 0xbd3f, 0x90 },
	{ 0xbd40, 0x58 },
	{ 0xbd41, 0x00 },
	{ 0x793b, 0x01 },
	{ 0xacc6, 0x00 },
	{ 0xacf5, 0x00 },
	{ 0x793b, 0x00 },
	{ 0x1f04, 0xb3 },
	{ 0x1f05, 0x01 },
	{ 0x1f06, 0x07 },
	{ 0x1f07, 0x66 },
	{ 0x1f08, 0x01 },
	{ 0x4d18, 0x00 },
	{ 0x4d19, 0x9d },
	{ 0x4d88, 0x00 },
	{ 0x4d89, 0x97 },
	{ 0x5c57, 0x0a },
	{ 0x5d94, 0x1f },
	{ 0x5d9e, 0x1f },
	{ 0x5e50, 0x23 },
	{ 0x5e51, 0x20 },
	{ 0x5e52, 0x07 },
	{ 0x5e53, 0x20 },
	{ 0x5e54, 0x07 },
	{ 0x5e55, 0x27 },
	{ 0x5e56, 0x0b },
	{ 0x5e57, 0x24 },
	{ 0x5e58, 0x0b },
	{ 0x5e60, 0x24 },
	{ 0x5e61, 0x24 },
	{ 0x5e62, 0x1b },
	{ 0x5e63, 0x23 },
	{ 0x5e64, 0x1b },
	{ 0x5e65, 0x28 },
	{ 0x5e66, 0x22 },
	{ 0x5e67, 0x28 },
	{ 0x5e68, 0x23 },
	{ 0x5e70, 0x25 },
	{ 0x5e71, 0x24 },
	{ 0x5e72, 0x20 },
	{ 0x5e73, 0x24 },
	{ 0x5e74, 0x20 },
	{ 0x5e75, 0x28 },
	{ 0x5e76, 0x27 },
	{ 0x5e77, 0x29 },
	{ 0x5e78, 0x24 },
	{ 0x5e80, 0x25 },
	{ 0x5e81, 0x25 },
	{ 0x5e82, 0x24 },
	{ 0x5e83, 0x25 },
	{ 0x5e84, 0x23 },
	{ 0x5e85, 0x2a },
	{ 0x5e86, 0x28 },
	{ 0x5e87, 0x2a },
	{ 0x5e88, 0x28 },
	{ 0x5e90, 0x24 },
	{ 0x5e91, 0x24 },
	{ 0x5e92, 0x28 },
	{ 0x5e93, 0x29 },
	{ 0x5e97, 0x25 },
	{ 0x5e98, 0x25 },
	{ 0x5e99, 0x2a },
	{ 0x5e9a, 0x2a },
	{ 0x5e9e, 0x3a },
	{ 0x5e9f, 0x3f },
	{ 0x5ea0, 0x17 },
	{ 0x5ea1, 0x3f },
	{ 0x5ea2, 0x17 },
	{ 0x5ea3, 0x32 },
	{ 0x5ea4, 0x10 },
	{ 0x5ea5, 0x33 },
	{ 0x5ea6, 0x10 },
	{ 0x5eae, 0x3d },
	{ 0x5eaf, 0x48 },
	{ 0x5eb0, 0x3b },
	{ 0x5eb1, 0x45 },
	{ 0x5eb2, 0x37 },
	{ 0x5eb3, 0x3a },
	{ 0x5eb4, 0x31 },
	{ 0x5eb5, 0x3a },
	{ 0x5eb6, 0x31 },
	{ 0x5ebe, 0x40 },
	{ 0x5ebf, 0x48 },
	{ 0x5ec0, 0x3f },
	{ 0x5ec1, 0x45 },
	{ 0x5ec2, 0x3f },
	{ 0x5ec3, 0x3a },
	{ 0x5ec4, 0x32 },
	{ 0x5ec5, 0x3a },
	{ 0x5ec6, 0x33 },
	{ 0x5ece, 0x4b },
	{ 0x5ecf, 0x4a },
	{ 0x5ed0, 0x48 },
	{ 0x5ed1, 0x4c },
	{ 0x5ed2, 0x45 },
	{ 0x5ed3, 0x3f },
	{ 0x5ed4, 0x3a },
	{ 0x5ed5, 0x3f },
	{ 0x5ed6, 0x3a },
	{ 0x5ede, 0x48 },
	{ 0x5edf, 0x45 },
	{ 0x5ee0, 0x3a },
	{ 0x5ee1, 0x3a },
	{ 0x5ee5, 0x4a },
	{ 0x5ee6, 0x4c },
	{ 0x5ee7, 0x3f },
	{ 0x5ee8, 0x3f },
	{ 0x5eec, 0x06 },
	{ 0x5eed, 0x06 },
	{ 0x5eee, 0x02 },
	{ 0x5eef, 0x06 },
	{ 0x5ef0, 0x01 },
	{ 0x5ef1, 0x09 },
	{ 0x5ef2, 0x05 },
	{ 0x5ef3, 0x06 },
	{ 0x5ef4, 0x04 },
	{ 0x5efc, 0x07 },
	{ 0x5efd, 0x09 },
	{ 0x5efe, 0x05 },
	{ 0x5eff, 0x08 },
	{ 0x5f00, 0x04 },
	{ 0x5f01, 0x09 },
	{ 0x5f02, 0x05 },
	{ 0x5f03, 0x09 },
	{ 0x5f04, 0x04 },
	{ 0x5f0c, 0x08 },
	{ 0x5f0d, 0x09 },
	{ 0x5f0e, 0x06 },
	{ 0x5f0f, 0x09 },
	{ 0x5f10, 0x06 },
	{ 0x5f11, 0x09 },
	{ 0x5f12, 0x09 },
	{ 0x5f13, 0x09 },
	{ 0x5f14, 0x06 },
	{ 0x5f1c, 0x09 },
	{ 0x5f1d, 0x09 },
	{ 0x5f1e, 0x09 },
	{ 0x5f1f, 0x09 },
	{ 0x5f20, 0x08 },
	{ 0x5f21, 0x09 },
	{ 0x5f22, 0x09 },
	{ 0x5f23, 0x09 },
	{ 0x5f24, 0x09 },
	{ 0x5f2c, 0x09 },
	{ 0x5f2d, 0x09 },
	{ 0x5f2e, 0x09 },
	{ 0x5f2f, 0x09 },
	{ 0x5f33, 0x09 },
	{ 0x5f34, 0x09 },
	{ 0x5f35, 0x09 },
	{ 0x5f36, 0x09 },
	{ 0x5f3a, 0x01 },
	{ 0x5f3d, 0x07 },
	{ 0x5f3f, 0x01 },
	{ 0x5f4b, 0x01 },
	{ 0x5f4d, 0x04 },
	{ 0x5f4f, 0x02 },
	{ 0x5f51, 0x02 },
	{ 0x5f5a, 0x02 },
	{ 0x5f5b, 0x01 },
	{ 0x5f5d, 0x03 },
	{ 0x5f5e, 0x07 },
	{ 0x5f5f, 0x01 },
	{ 0x5f60, 0x01 },
	{ 0x5f61, 0x01 },
	{ 0x5f6a, 0x01 },
	{ 0x5f6c, 0x01 },
	{ 0x5f6d, 0x01 },
	{ 0x5f6e, 0x04 },
	{ 0x5f70, 0x02 },
	{ 0x5f72, 0x02 },
	{ 0x5f7a, 0x01 },
	{ 0x5f7b, 0x03 },
	{ 0x5f7c, 0x01 },
	{ 0x5f7d, 0x01 },
	{ 0x5f82, 0x01 },
	{ 0x60c6, 0x4a },
	{ 0x60c8, 0x4a },
	{ 0x60d6, 0x4a },
	{ 0x60d8, 0x4a },
	{ 0x62e4, 0x33 },
	{ 0x62e9, 0x33 },
	{ 0x62ee, 0x1c },
	{ 0x62ef, 0x33 },
	{ 0x62f3, 0x33 },
	{ 0x62f6, 0x1c },
	{ 0x33f2, 0x01 },
	{ 0x1f04, 0xa3 },
	{ 0x1f05, 0x01 },
	{ 0x406e, 0x00 },
	{ 0x406f, 0x08 },
	{ 0x4d08, 0x00 },
	{ 0x4d09, 0x2c },
	{ 0x4d0e, 0x00 },
	{ 0x4d0f, 0x64 },
	{ 0x4d18, 0x00 },
	{ 0x4d19, 0xb1 },
	{ 0x4d1e, 0x00 },
	{ 0x4d1f, 0xcb },
	{ 0x4d3a, 0x00 },
	{ 0x4d3b, 0x91 },
	{ 0x4d40, 0x00 },
	{ 0x4d41, 0x64 },
	{ 0x4d4c, 0x00 },
	{ 0x4d4d, 0xe8 },
	{ 0x4d52, 0x00 },
	{ 0x4d53, 0xcb },
	{ 0x4d78, 0x00 },
	{ 0x4d79, 0x2c },
	{ 0x4d7e, 0x00 },
	{ 0x4d7f, 0x64 },
	{ 0x4d88, 0x00 },
	{ 0x4d89, 0xab },
	{ 0x4d8e, 0x00 },
	{ 0x4d8f, 0xcb },
	{ 0x4da6, 0x00 },
	{ 0x4da7, 0xe7 },
	{ 0x4dac, 0x00 },
	{ 0x4dad, 0xcb },
	{ 0x5b98, 0x00 },
	{ 0x5c52, 0x05 },
	{ 0x5c57, 0x09 },
	{ 0x5d94, 0x0a },
	{ 0x5d9e, 0x0a },
	{ 0x5e50, 0x22 },
	{ 0x5e51, 0x22 },
	{ 0x5e52, 0x07 },
	{ 0x5e53, 0x20 },
	{ 0x5e54, 0x06 },
	{ 0x5e55, 0x23 },
	{ 0x5e56, 0x0a },
	{ 0x5e57, 0x23 },
	{ 0x5e58, 0x0a },
	{ 0x5e60, 0x25 },
	{ 0x5e61, 0x29 },
	{ 0x5e62, 0x1c },
	{ 0x5e63, 0x26 },
	{ 0x5e64, 0x1c },
	{ 0x5e65, 0x2d },
	{ 0x5e66, 0x1e },
	{ 0x5e67, 0x2a },
	{ 0x5e68, 0x1e },
	{ 0x5e70, 0x26 },
	{ 0x5e71, 0x26 },
	{ 0x5e72, 0x22 },
	{ 0x5e73, 0x23 },
	{ 0x5e74, 0x20 },
	{ 0x5e75, 0x28 },
	{ 0x5e76, 0x23 },
	{ 0x5e77, 0x28 },
	{ 0x5e78, 0x23 },
	{ 0x5e80, 0x28 },
	{ 0x5e81, 0x28 },
	{ 0x5e82, 0x29 },
	{ 0x5e83, 0x27 },
	{ 0x5e84, 0x26 },
	{ 0x5e85, 0x2a },
	{ 0x5e86, 0x2d },
	{ 0x5e87, 0x2a },
	{ 0x5e88, 0x2a },
	{ 0x5e90, 0x26 },
	{ 0x5e91, 0x23 },
	{ 0x5e92, 0x28 },
	{ 0x5e93, 0x28 },
	{ 0x5e97, 0x2f },
	{ 0x5e98, 0x2e },
	{ 0x5e99, 0x32 },
	{ 0x5e9a, 0x32 },
	{ 0x5e9e, 0x50 },
	{ 0x5e9f, 0x50 },
	{ 0x5ea0, 0x1e },
	{ 0x5ea1, 0x50 },
	{ 0x5ea2, 0x1d },
	{ 0x5ea3, 0x3e },
	{ 0x5ea4, 0x14 },
	{ 0x5ea5, 0x3e },
	{ 0x5ea6, 0x14 },
	{ 0x5eae, 0x58 },
	{ 0x5eaf, 0x5e },
	{ 0x5eb0, 0x4b },
	{ 0x5eb1, 0x5a },
	{ 0x5eb2, 0x4b },
	{ 0x5eb3, 0x4c },
	{ 0x5eb4, 0x3a },
	{ 0x5eb5, 0x4c },
	{ 0x5eb6, 0x38 },
	{ 0x5ebe, 0x56 },
	{ 0x5ebf, 0x57 },
	{ 0x5ec0, 0x50 },
	{ 0x5ec1, 0x55 },
	{ 0x5ec2, 0x50 },
	{ 0x5ec3, 0x46 },
	{ 0x5ec4, 0x3e },
	{ 0x5ec5, 0x46 },
	{ 0x5ec6, 0x3e },
	{ 0x5ece, 0x5a },
	{ 0x5ecf, 0x5f },
	{ 0x5ed0, 0x5e },
	{ 0x5ed1, 0x5a },
	{ 0x5ed2, 0x5a },
	{ 0x5ed3, 0x50 },
	{ 0x5ed4, 0x4c },
	{ 0x5ed5, 0x50 },
	{ 0x5ed6, 0x4c },
	{ 0x5ede, 0x57 },
	{ 0x5edf, 0x55 },
	{ 0x5ee0, 0x46 },
	{ 0x5ee1, 0x46 },
	{ 0x5ee5, 0x73 },
	{ 0x5ee6, 0x6e },
	{ 0x5ee7, 0x5f },
	{ 0x5ee8, 0x5a },
	{ 0x5eec, 0x0a },
	{ 0x5eed, 0x0a },
	{ 0x5eee, 0x0f },
	{ 0x5eef, 0x0a },
	{ 0x5ef0, 0x0e },
	{ 0x5ef1, 0x08 },
	{ 0x5ef2, 0x0c },
	{ 0x5ef3, 0x0c },
	{ 0x5ef4, 0x0f },
	{ 0x5efc, 0x0a },
	{ 0x5efd, 0x0a },
	{ 0x5efe, 0x14 },
	{ 0x5eff, 0x0a },
	{ 0x5f00, 0x14 },
	{ 0x5f01, 0x0a },
	{ 0x5f02, 0x14 },
	{ 0x5f03, 0x0a },
	{ 0x5f04, 0x19 },
	{ 0x5f0c, 0x0a },
	{ 0x5f0d, 0x0a },
	{ 0x5f0e, 0x0a },
	{ 0x5f0f, 0x05 },
	{ 0x5f10, 0x0a },
	{ 0x5f11, 0x06 },
	{ 0x5f12, 0x08 },
	{ 0x5f13, 0x0a },
	{ 0x5f14, 0x0c },
	{ 0x5f1c, 0x0a },
	{ 0x5f1d, 0x0a },
	{ 0x5f1e, 0x0a },
	{ 0x5f1f, 0x0a },
	{ 0x5f20, 0x0a },
	{ 0x5f21, 0x0a },
	{ 0x5f22, 0x0a },
	{ 0x5f23, 0x0a },
	{ 0x5f24, 0x0a },
	{ 0x5f2c, 0x0a },
	{ 0x5f2d, 0x05 },
	{ 0x5f2e, 0x06 },
	{ 0x5f2f, 0x0a },
	{ 0x5f33, 0x0a },
	{ 0x5f34, 0x0a },
	{ 0x5f35, 0x0a },
	{ 0x5f36, 0x0a },
	{ 0x5f3a, 0x00 },
	{ 0x5f3d, 0x02 },
	{ 0x5f3f, 0x0a },
	{ 0x5f4a, 0x0a },
	{ 0x5f4b, 0x0a },
	{ 0x5f4d, 0x0f },
	{ 0x5f4f, 0x00 },
	{ 0x5f51, 0x00 },
	{ 0x5f5a, 0x00 },
	{ 0x5f5b, 0x00 },
	{ 0x5f5d, 0x0a },
	{ 0x5f5e, 0x02 },
	{ 0x5f5f, 0x0a },
	{ 0x5f60, 0x0a },
	{ 0x5f61, 0x00 },
	{ 0x5f6a, 0x00 },
	{ 0x5f6c, 0x0a },
	{ 0x5f6d, 0x06 },
	{ 0x5f6e, 0x0f },
	{ 0x5f70, 0x00 },
	{ 0x5f72, 0x00 },
	{ 0x5f7a, 0x00 },
	{ 0x5f7b, 0x0a },
	{ 0x5f7c, 0x0a },
	{ 0x5f7d, 0x00 },
	{ 0x5f82, 0x06 },
	{ 0x60c6, 0x36 },
	{ 0x60c8, 0x36 },
	{ 0x60d6, 0x36 },
	{ 0x60d8, 0x36 },
	{ 0x62df, 0x56 },
	{ 0x62e0, 0x52 },
	{ 0x62e4, 0x38 },
	{ 0x62e5, 0x51 },
	{ 0x62e9, 0x35 },
	{ 0x62ea, 0x54 },
	{ 0x62ee, 0x1d },
	{ 0x62ef, 0x38 },
	{ 0x62f3, 0x33 },
	{ 0x62f6, 0x26 },
	{ 0x6412, 0x1e },
	{ 0x6413, 0x1e },
	{ 0x6414, 0x1e },
	{ 0x6415, 0x1e },
	{ 0x6416, 0x1e },
	{ 0x6417, 0x1e },
	{ 0x6418, 0x1e },
	{ 0x641a, 0x1e },
	{ 0x641b, 0x1e },
	{ 0x641c, 0x1e },
	{ 0x641d, 0x1e },
	{ 0x641e, 0x1e },
	{ 0x641f, 0x1e },
	{ 0x6420, 0x1e },
	{ 0x6421, 0x1e },
	{ 0x6422, 0x1e },
	{ 0x6424, 0x1e },
	{ 0x6425, 0x1e },
	{ 0x6426, 0x1e },
	{ 0x6427, 0x1e },
	{ 0x6428, 0x1e },
	{ 0x6429, 0x1e },
	{ 0x642a, 0x1e },
	{ 0x642b, 0x1e },
	{ 0x642c, 0x1e },
	{ 0x642e, 0x1e },
	{ 0x642f, 0x1e },
	{ 0x6430, 0x1e },
	{ 0x6431, 0x1e },
	{ 0x6432, 0x1e },
	{ 0x6433, 0x1e },
	{ 0x6434, 0x1e },
	{ 0x6435, 0x1e },
	{ 0x6436, 0x1e },
	{ 0x6438, 0x1e },
	{ 0x6439, 0x1e },
	{ 0x643a, 0x1e },
	{ 0x643b, 0x1e },
	{ 0x643d, 0x1e },
	{ 0x643e, 0x1e },
	{ 0x643f, 0x1e },
	{ 0x6441, 0x1e },
	{ 0x33f2, 0x02 },
	{ 0x1f08, 0x00 },
	{ 0xa307, 0x30 },
	{ 0xa309, 0x30 },
	{ 0xa30b, 0x30 },
	{ 0xa406, 0x03 },
	{ 0xa407, 0x48 },
	{ 0xa408, 0x03 },
	{ 0xa409, 0x48 },
	{ 0xa40a, 0x03 },
	{ 0xa40b, 0x48 },
	{ 0x825b, 0x01 },
	{ 0xec12, 0x01 },
	{ 0xec14, 0x3f },
	{ 0xec15, 0x00 },
	{ 0xec16, 0x20 },
	{ 0xec17, 0x00 },
	{ 0xec18, 0x1f },
	{ 0xec19, 0xff },
	{ 0xec1a, 0x00 },
	{ 0xec1b, 0x00 },
	{ 0x0101, 0x00 },
	{ 0x0106, 0x01 },
	{ 0x305c, 0x00 },
	{ 0x32d2, 0x01 },
	{ 0x86a9, 0x4e },
	{ 0x0100, 0x00 },
};

/* Sensor mode registers: 4096x3072 2x2 binned, 30 fps */
static const struct imx766_reg mode_4096x3072_regs[] = {
	{ 0x0112, 0x0a },
	{ 0x0113, 0x0a },
	{ 0x0114, 0x02 },
	{ 0x0342, 0x3d },
	{ 0x0343, 0x00 },
	{ 0x0340, 0x0c },
	{ 0x0341, 0x6c },
	{ 0x0344, 0x00 },
	{ 0x0345, 0x00 },
	{ 0x0346, 0x00 },
	{ 0x0347, 0x00 },
	{ 0x0348, 0x1f },
	{ 0x0349, 0xff },
	{ 0x034a, 0x17 },
	{ 0x034b, 0xff },
	{ 0x0900, 0x01 },
	{ 0x0901, 0x22 },
	{ 0x0902, 0x08 },
	{ 0x3005, 0x02 },
	{ 0x3120, 0x04 },
	{ 0x3121, 0x01 },
	{ 0x3200, 0x41 },
	{ 0x3201, 0x41 },
	{ 0x32d6, 0x00 },
	{ 0x0408, 0x00 },
	{ 0x0409, 0x00 },
	{ 0x040a, 0x00 },
	{ 0x040b, 0x00 },
	{ 0x040c, 0x10 },
	{ 0x040d, 0x00 },
	{ 0x040e, 0x0c },
	{ 0x040f, 0x00 },
	{ 0x034c, 0x10 },
	{ 0x034d, 0x00 },
	{ 0x034e, 0x0c },
	{ 0x034f, 0x00 },
	{ 0x0301, 0x05 },
	{ 0x0303, 0x04 },
	{ 0x0305, 0x03 },
	{ 0x0306, 0x01 },
	{ 0x0307, 0x24 },
	{ 0x030b, 0x04 },
	{ 0x030d, 0x02 },
	{ 0x030e, 0x01 },
	{ 0x030f, 0x9c },
	{ 0x30cb, 0x00 },
	{ 0x30cc, 0x10 },
	{ 0x30cd, 0x00 },
	{ 0x30ce, 0x03 },
	{ 0x30cf, 0x00 },
	{ 0x319c, 0x01 },
	{ 0x3800, 0x01 },
	{ 0x3801, 0x01 },
	{ 0x3802, 0x02 },
	{ 0x3847, 0x03 },
	{ 0x38b0, 0x00 },
	{ 0x38b1, 0x00 },
	{ 0x38b2, 0x00 },
	{ 0x38b3, 0x00 },
	{ 0x38c4, 0x01 },
	{ 0x38c5, 0x2c },
	{ 0x4c3a, 0x02 },
	{ 0x4c3b, 0xd2 },
	{ 0x4c68, 0x04 },
	{ 0x4c69, 0x7e },
	{ 0x4cf8, 0x07 },
	{ 0x4cf9, 0x9e },
	{ 0x4db8, 0x08 },
	{ 0x4db9, 0x98 },
	{ 0x0202, 0x0c },
	{ 0x0203, 0x3c },
	{ 0x0224, 0x01 },
	{ 0x0225, 0xf4 },
	{ 0x313a, 0x01 },
	{ 0x313b, 0xf4 },
	{ 0x3803, 0x00 },
	{ 0x3804, 0x17 },
	{ 0x3805, 0xc0 },
	{ 0x0204, 0x00 },
	{ 0x0205, 0x00 },
	{ 0x020e, 0x01 },
	{ 0x020f, 0x00 },
	{ 0x0216, 0x00 },
	{ 0x0217, 0x00 },
	{ 0x0218, 0x01 },
	{ 0x0219, 0x00 },
	{ 0x313c, 0x00 },
	{ 0x313d, 0x00 },
	{ 0x313e, 0x01 },
	{ 0x313f, 0x00 },
	{ 0x0860, 0x01 },
	{ 0x0861, 0x2d },
	{ 0x0862, 0x01 },
	{ 0x0863, 0x2d },
	{ 0x30b4, 0x01 },
	{ 0x3066, 0x00 },
	{ 0x3067, 0x30 },
	{ 0x3068, 0x00 },
	{ 0x3069, 0x30 },
	{ 0x33d0, 0x00 },
	{ 0x33d1, 0x00 },
	{ 0x33d4, 0x01 },
	{ 0x33dc, 0x0a },
	{ 0x33dd, 0x0a },
	{ 0x33de, 0x0a },
	{ 0x33df, 0x0a },
	{ 0x3070, 0x01 },
	{ 0x3077, 0x01 },
	{ 0x3078, 0x30 },
	{ 0x3079, 0x01 },
	{ 0x307a, 0x30 },
	{ 0x307b, 0x01 },
	{ 0x3080, 0x02 },
	{ 0x3087, 0x02 },
	{ 0x3088, 0x30 },
	{ 0x3089, 0x02 },
	{ 0x308a, 0x30 },
	{ 0x308b, 0x02 },
	{ 0x3901, 0x2b },
	{ 0x3902, 0x00 },
	{ 0x3903, 0x12 },
	{ 0x3905, 0x2b },
	{ 0x3906, 0x01 },
	{ 0x3907, 0x12 },
	{ 0x3909, 0x2b },
	{ 0x390a, 0x02 },
	{ 0x390b, 0x12 },
	{ 0x3911, 0x00 },
};

/*
 * Sensor mode registers: 2048x1536 4x4 binned, 30 fps.  Vendor mode 3
 * (2048x1152) with the vertical crop opened to the full array height.
 */
static const struct imx766_reg mode_2048x1536_regs[] = {
	{ 0x0112, 0x0a },
	{ 0x0113, 0x0a },
	{ 0x0114, 0x02 },
	{ 0x0342, 0x67 },
	{ 0x0343, 0x50 },
	{ 0x0340, 0x08 },
	{ 0x0341, 0x50 },
	{ 0x0344, 0x00 },
	{ 0x0345, 0x00 },
	{ 0x0346, 0x00 },
	{ 0x0347, 0x00 },
	{ 0x0348, 0x1f },
	{ 0x0349, 0xff },
	{ 0x034a, 0x17 },
	{ 0x034b, 0xff },
	{ 0x0900, 0x01 },
	{ 0x0901, 0x44 },
	{ 0x0902, 0x08 },
	{ 0x3005, 0x02 },
	{ 0x3120, 0x04 },
	{ 0x3121, 0x00 },
	{ 0x3200, 0x43 },
	{ 0x3201, 0x43 },
	{ 0x32d6, 0x00 },
	{ 0x0408, 0x00 },
	{ 0x0409, 0x00 },
	{ 0x040a, 0x00 },
	{ 0x040b, 0x00 },
	{ 0x040c, 0x08 },
	{ 0x040d, 0x00 },
	{ 0x040e, 0x06 },
	{ 0x040f, 0x00 },
	{ 0x034c, 0x08 },
	{ 0x034d, 0x00 },
	{ 0x034e, 0x06 },
	{ 0x034f, 0x00 },
	{ 0x0301, 0x05 },
	{ 0x0303, 0x02 },
	{ 0x0305, 0x03 },
	{ 0x0306, 0x00 },
	{ 0x0307, 0xa5 },
	{ 0x030b, 0x08 },
	{ 0x030d, 0x02 },
	{ 0x030e, 0x01 },
	{ 0x030f, 0x02 },
	{ 0x30cb, 0x00 },
	{ 0x30cc, 0x10 },
	{ 0x30cd, 0x00 },
	{ 0x30ce, 0x03 },
	{ 0x30cf, 0x00 },
	{ 0x319c, 0x01 },
	{ 0x3800, 0x01 },
	{ 0x3801, 0x01 },
	{ 0x3802, 0x02 },
	{ 0x3847, 0x03 },
	{ 0x38b0, 0x01 },
	{ 0x38b1, 0x2c },
	{ 0x38b2, 0x01 },
	{ 0x38b3, 0x2c },
	{ 0x38c4, 0x01 },
	{ 0x38c5, 0x90 },
	{ 0x4c3a, 0x02 },
	{ 0x4c3b, 0xd2 },
	{ 0x4c68, 0x04 },
	{ 0x4c69, 0x7e },
	{ 0x4cf8, 0x07 },
	{ 0x4cf9, 0x9e },
	{ 0x4db8, 0x19 },
	{ 0x4db9, 0xd4 },
	{ 0x0202, 0x08 },
	{ 0x0203, 0x20 },
	{ 0x0224, 0x01 },
	{ 0x0225, 0xf4 },
	{ 0x313a, 0x01 },
	{ 0x313b, 0xf4 },
	{ 0x3803, 0x00 },
	{ 0x3804, 0x17 },
	{ 0x3805, 0xc0 },
	{ 0x0204, 0x00 },
	{ 0x0205, 0x00 },
	{ 0x020e, 0x01 },
	{ 0x020f, 0x00 },
	{ 0x0216, 0x00 },
	{ 0x0217, 0x00 },
	{ 0x0218, 0x01 },
	{ 0x0219, 0x00 },
	{ 0x313c, 0x00 },
	{ 0x313d, 0x00 },
	{ 0x313e, 0x01 },
	{ 0x313f, 0x00 },
	{ 0x0860, 0x01 },
	{ 0x0861, 0x2d },
	{ 0x0862, 0x01 },
	{ 0x0863, 0x2d },
	{ 0x30b4, 0x01 },
	{ 0x3066, 0x00 },
	{ 0x3067, 0x30 },
	{ 0x3068, 0x00 },
	{ 0x3069, 0x30 },
	{ 0x33d0, 0x00 },
	{ 0x33d1, 0x00 },
	{ 0x33d4, 0x01 },
	{ 0x33dc, 0x0a },
	{ 0x33dd, 0x0a },
	{ 0x33de, 0x0a },
	{ 0x33df, 0x0a },
	{ 0x3070, 0x01 },
	{ 0x3077, 0x01 },
	{ 0x3078, 0x30 },
	{ 0x3079, 0x01 },
	{ 0x307a, 0x30 },
	{ 0x307b, 0x01 },
	{ 0x3080, 0x02 },
	{ 0x3087, 0x02 },
	{ 0x3088, 0x30 },
	{ 0x3089, 0x02 },
	{ 0x308a, 0x30 },
	{ 0x308b, 0x02 },
	{ 0x3901, 0x2b },
	{ 0x3902, 0x00 },
	{ 0x3903, 0x12 },
	{ 0x3905, 0x2b },
	{ 0x3906, 0x01 },
	{ 0x3907, 0x12 },
	{ 0x3909, 0x2b },
	{ 0x390a, 0x02 },
	{ 0x390b, 0x12 },
	{ 0x3911, 0x00 },
};

/* Supported sensor mode configurations, default first */
static const struct imx766_mode supported_modes[] = {
	{
	.width = 2048,
	.height = 1536,
	.hblank = 26448 - 2048,	/* line length 26448 */
	.vblank = 2128 - 1536,	/* frame length 2128 -> 30.0 fps */
	.vblank_min = 2128 - 1536,
	.vblank_max = 0xffff - 1536,
	.pclk = 1689600000,	/* 19.2 MHz / 3 * 165 / 2 * 3.2 */
	.link_freq_idx = 1,
	.code = MEDIA_BUS_FMT_SRGGB10_1X10,
	.reg_list = {
		.num_of_regs = ARRAY_SIZE(mode_2048x1536_regs),
		.regs = mode_2048x1536_regs,
	},
	},
	{
	.width = 4096,
	.height = 3072,
	.hblank = 15616 - 4096,	/* line length 15616 */
	.vblank = 3180 - 3072,	/* frame length 3180 -> 30.1 fps */
	.vblank_min = 3180 - 3072,
	.vblank_max = 0xffff - 3072,
	.pclk = 1495040000,	/* 19.2 MHz / 3 * 292 / 20 * 16 */
	.link_freq_idx = 0,
	.code = MEDIA_BUS_FMT_SRGGB10_1X10,	/* native RGGB array, no mirror/flip */
	.reg_list = {
		.num_of_regs = ARRAY_SIZE(mode_4096x3072_regs),
		.regs = mode_4096x3072_regs,
	},
	},
};

/**
 * to_imx766() - imx766 V4L2 sub-device to imx766 device.
 * @subdev: pointer to imx766 V4L2 sub-device
 *
 * Return: pointer to imx766 device
 */
static inline struct imx766 *to_imx766(struct v4l2_subdev *subdev)
{
	return container_of(subdev, struct imx766, sd);
}

/**
 * imx766_read_reg() - Read registers.
 * @imx766: pointer to imx766 device
 * @reg: register address
 * @len: length of bytes to read. Max supported bytes is 4
 * @val: pointer to register value to be filled.
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_read_reg(struct imx766 *imx766, u16 reg, u32 len, u32 *val)
{
	struct i2c_client *client = v4l2_get_subdevdata(&imx766->sd);
	struct i2c_msg msgs[2] = {0};
	u8 addr_buf[2] = {0};
	u8 data_buf[4] = {0};
	int ret;

	if (WARN_ON(len > 4))
		return -EINVAL;

	put_unaligned_be16(reg, addr_buf);

	/* Write register address */
	msgs[0].addr = client->addr;
	msgs[0].flags = 0;
	msgs[0].len = ARRAY_SIZE(addr_buf);
	msgs[0].buf = addr_buf;

	/* Read data from register */
	msgs[1].addr = client->addr;
	msgs[1].flags = I2C_M_RD;
	msgs[1].len = len;
	msgs[1].buf = &data_buf[4 - len];

	ret = i2c_transfer(client->adapter, msgs, ARRAY_SIZE(msgs));
	if (ret != ARRAY_SIZE(msgs))
		return -EIO;

	*val = get_unaligned_be32(data_buf);

	return 0;
}

/**
 * imx766_write_reg() - Write register
 * @imx766: pointer to imx766 device
 * @reg: register address
 * @len: length of bytes. Max supported bytes is 4
 * @val: register value
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_write_reg(struct imx766 *imx766, u16 reg, u32 len, u32 val)
{
	struct i2c_client *client = v4l2_get_subdevdata(&imx766->sd);
	u8 buf[6] = {0};

	if (WARN_ON(len > 4))
		return -EINVAL;

	put_unaligned_be16(reg, buf);
	put_unaligned_be32(val << (8 * (4 - len)), buf + 2);
	if (i2c_master_send(client, buf, len + 2) != len + 2)
		return -EIO;

	return 0;
}

/**
 * imx766_write_regs() - Write a list of registers
 * @imx766: pointer to imx766 device
 * @regs: list of registers to be written
 * @len: length of registers array
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_write_regs(struct imx766 *imx766,
			     const struct imx766_reg *regs, u32 len)
{
	unsigned int i;
	int ret;

	for (i = 0; i < len; i++) {
		ret = imx766_write_reg(imx766, regs[i].address, 1, regs[i].val);
		if (ret)
			return ret;
	}

	return 0;
}

/**
 * imx766_update_controls() - Update control ranges based on streaming mode
 * @imx766: pointer to imx766 device
 * @mode: pointer to imx766_mode sensor mode
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_update_controls(struct imx766 *imx766,
				  const struct imx766_mode *mode)
{
	int ret;

	ret = __v4l2_ctrl_s_ctrl(imx766->link_freq_ctrl, mode->link_freq_idx);
	if (ret)
		return ret;

	ret = __v4l2_ctrl_modify_range(imx766->pclk_ctrl, mode->pclk,
				       mode->pclk, 1, mode->pclk);
	if (ret)
		return ret;

	ret = __v4l2_ctrl_modify_range(imx766->hblank_ctrl, mode->hblank,
				       mode->hblank, 1, mode->hblank);
	if (ret)
		return ret;

	return __v4l2_ctrl_modify_range(imx766->vblank_ctrl, mode->vblank_min,
					mode->vblank_max, 1, mode->vblank);
}

/**
 * imx766_update_exp_gain() - Set updated exposure and gain
 * @imx766: pointer to imx766 device
 * @exposure: updated exposure value
 * @gain: updated analog gain value
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_update_exp_gain(struct imx766 *imx766, u32 exposure, u32 gain)
{
	u32 lpfr;
	int ret;

	lpfr = imx766->vblank + imx766->cur_mode->height;

	dev_dbg(imx766->dev, "Set exp %u, analog gain %u, lpfr %u\n",
		exposure, gain, lpfr);

	ret = imx766_write_reg(imx766, IMX766_REG_HOLD, 1, 1);
	if (ret)
		return ret;

	ret = imx766_write_reg(imx766, IMX766_REG_LPFR, 2, lpfr);
	if (ret)
		goto error_release_group_hold;

	ret = imx766_write_reg(imx766, IMX766_REG_EXPOSURE_CIT, 2, exposure);
	if (ret)
		goto error_release_group_hold;

	ret = imx766_write_reg(imx766, IMX766_REG_AGAIN, 2, gain);

error_release_group_hold:
	imx766_write_reg(imx766, IMX766_REG_HOLD, 1, 0);

	return ret;
}

/**
 * imx766_set_ctrl() - Set subdevice control
 * @ctrl: pointer to v4l2_ctrl structure
 *
 * Supported controls:
 * - V4L2_CID_VBLANK
 * - cluster controls:
 *   - V4L2_CID_ANALOGUE_GAIN
 *   - V4L2_CID_EXPOSURE
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_set_ctrl(struct v4l2_ctrl *ctrl)
{
	struct imx766 *imx766 =
		container_of(ctrl->handler, struct imx766, ctrl_handler);
	u32 analog_gain;
	u32 exposure;
	int ret;

	switch (ctrl->id) {
	case V4L2_CID_VBLANK:
		imx766->vblank = imx766->vblank_ctrl->val;

		dev_dbg(imx766->dev, "Received vblank %u, new lpfr %u\n",
			imx766->vblank,
			imx766->vblank + imx766->cur_mode->height);

		ret = __v4l2_ctrl_modify_range(imx766->exp_ctrl,
					       IMX766_EXPOSURE_MIN,
					       imx766->vblank +
					       imx766->cur_mode->height -
					       IMX766_EXPOSURE_OFFSET,
					       1, IMX766_EXPOSURE_DEFAULT);
		break;
	case V4L2_CID_EXPOSURE:
		/* Set controls only if sensor is in power on state */
		if (!pm_runtime_get_if_in_use(imx766->dev))
			return 0;

		exposure = ctrl->val;
		analog_gain = imx766->again_ctrl->val;

		dev_dbg(imx766->dev, "Received exp %u, analog gain %u\n",
			exposure, analog_gain);

		ret = imx766_update_exp_gain(imx766, exposure, analog_gain);

		pm_runtime_put(imx766->dev);

		break;
	case V4L2_CID_PIXEL_RATE:
	case V4L2_CID_HBLANK:
	case V4L2_CID_LINK_FREQ:
		/* read-only, changed on a mode switch; nothing to write */
		ret = 0;
		break;
	default:
		dev_err(imx766->dev, "Invalid control %d\n", ctrl->id);
		ret = -EINVAL;
	}

	return ret;
}

/* V4l2 subdevice control ops*/
static const struct v4l2_ctrl_ops imx766_ctrl_ops = {
	.s_ctrl = imx766_set_ctrl,
};

/**
 * imx766_enum_mbus_code() - Enumerate V4L2 sub-device mbus codes
 * @sd: pointer to imx766 V4L2 sub-device structure
 * @sd_state: V4L2 sub-device configuration
 * @code: V4L2 sub-device code enumeration need to be filled
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_enum_mbus_code(struct v4l2_subdev *sd,
				 struct v4l2_subdev_state *sd_state,
				 struct v4l2_subdev_mbus_code_enum *code)
{
	if (code->index > 0)
		return -EINVAL;

	code->code = supported_modes[0].code;

	return 0;
}

/**
 * imx766_enum_frame_size() - Enumerate V4L2 sub-device frame sizes
 * @sd: pointer to imx766 V4L2 sub-device structure
 * @sd_state: V4L2 sub-device configuration
 * @fsize: V4L2 sub-device size enumeration need to be filled
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_enum_frame_size(struct v4l2_subdev *sd,
				  struct v4l2_subdev_state *sd_state,
				  struct v4l2_subdev_frame_size_enum *fsize)
{
	if (fsize->index >= ARRAY_SIZE(supported_modes))
		return -EINVAL;

	if (fsize->code != supported_modes[0].code)
		return -EINVAL;

	fsize->min_width = supported_modes[fsize->index].width;
	fsize->max_width = fsize->min_width;
	fsize->min_height = supported_modes[fsize->index].height;
	fsize->max_height = fsize->min_height;

	return 0;
}

/**
 * imx766_fill_pad_format() - Fill subdevice pad format
 *                            from selected sensor mode
 * @imx766: pointer to imx766 device
 * @mode: pointer to imx766_mode sensor mode
 * @fmt: V4L2 sub-device format need to be filled
 */
static void imx766_fill_pad_format(struct imx766 *imx766,
				   const struct imx766_mode *mode,
				   struct v4l2_subdev_format *fmt)
{
	fmt->format.width = mode->width;
	fmt->format.height = mode->height;
	fmt->format.code = mode->code;
	fmt->format.field = V4L2_FIELD_NONE;
	fmt->format.colorspace = V4L2_COLORSPACE_RAW;
	fmt->format.ycbcr_enc = V4L2_YCBCR_ENC_DEFAULT;
	fmt->format.quantization = V4L2_QUANTIZATION_DEFAULT;
	fmt->format.xfer_func = V4L2_XFER_FUNC_NONE;
}

/**
 * imx766_get_pad_format() - Get subdevice pad format
 * @sd: pointer to imx766 V4L2 sub-device structure
 * @sd_state: V4L2 sub-device configuration
 * @fmt: V4L2 sub-device format need to be set
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_get_pad_format(struct v4l2_subdev *sd,
				 struct v4l2_subdev_state *sd_state,
				 struct v4l2_subdev_format *fmt)
{
	struct imx766 *imx766 = to_imx766(sd);

	mutex_lock(&imx766->mutex);

	if (fmt->which == V4L2_SUBDEV_FORMAT_TRY) {
		struct v4l2_mbus_framefmt *framefmt;

		framefmt = v4l2_subdev_state_get_format(sd_state, fmt->pad);
		fmt->format = *framefmt;
	} else {
		imx766_fill_pad_format(imx766, imx766->cur_mode, fmt);
	}

	mutex_unlock(&imx766->mutex);

	return 0;
}

/**
 * imx766_set_pad_format() - Set subdevice pad format
 * @sd: pointer to imx766 V4L2 sub-device structure
 * @sd_state: V4L2 sub-device configuration
 * @fmt: V4L2 sub-device format need to be set
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_set_pad_format(struct v4l2_subdev *sd,
				 struct v4l2_subdev_state *sd_state,
				 struct v4l2_subdev_format *fmt)
{
	struct imx766 *imx766 = to_imx766(sd);
	const struct imx766_mode *mode;
	int ret = 0;

	mutex_lock(&imx766->mutex);

	mode = v4l2_find_nearest_size(supported_modes,
				      ARRAY_SIZE(supported_modes),
				      width, height,
				      fmt->format.width, fmt->format.height);
	imx766_fill_pad_format(imx766, mode, fmt);

	if (fmt->which == V4L2_SUBDEV_FORMAT_TRY) {
		struct v4l2_mbus_framefmt *framefmt;

		framefmt = v4l2_subdev_state_get_format(sd_state, fmt->pad);
		*framefmt = fmt->format;
	} else {
		const struct imx766_mode *old = imx766->cur_mode;

		/* the vblank -> exposure range update reads cur_mode */
		imx766->cur_mode = mode;
		ret = imx766_update_controls(imx766, mode);
		if (ret)
			imx766->cur_mode = old;
	}

	mutex_unlock(&imx766->mutex);

	return ret;
}

/**
 * imx766_init_state() - Initialize sub-device state
 * @sd: pointer to imx766 V4L2 sub-device structure
 * @sd_state: V4L2 sub-device configuration
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_init_state(struct v4l2_subdev *sd,
			     struct v4l2_subdev_state *sd_state)
{
	struct imx766 *imx766 = to_imx766(sd);
	struct v4l2_subdev_format fmt = { 0 };

	fmt.which = sd_state ? V4L2_SUBDEV_FORMAT_TRY : V4L2_SUBDEV_FORMAT_ACTIVE;
	imx766_fill_pad_format(imx766, &supported_modes[0], &fmt);

	return imx766_set_pad_format(sd, sd_state, &fmt);
}

/**
 * imx766_start_streaming() - Start sensor stream
 * @imx766: pointer to imx766 device
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_start_streaming(struct imx766 *imx766)
{
	const struct imx766_reg_list *reg_list;
	int ret;

	ret = imx766_write_regs(imx766, common_regs, ARRAY_SIZE(common_regs));
	if (ret) {
		dev_err(imx766->dev, "fail to write common registers\n");
		return ret;
	}

	/* Write sensor mode registers */
	reg_list = &imx766->cur_mode->reg_list;
	ret = imx766_write_regs(imx766, reg_list->regs,
				reg_list->num_of_regs);
	if (ret) {
		dev_err(imx766->dev, "fail to write initial registers\n");
		return ret;
	}

	/* Setup handler will write actual exposure and gain */
	ret =  __v4l2_ctrl_handler_setup(imx766->sd.ctrl_handler);
	if (ret) {
		dev_err(imx766->dev, "fail to setup handler\n");
		return ret;
	}

	/* Delay is required before streaming*/
	usleep_range(7400, 8000);

	ret = imx766_write_reg(imx766, IMX766_REG_ORIENTATION, 1,
			       IMX766_ORIENTATION);
	if (ret)
		return ret;

	/* Start streaming */
	ret = imx766_write_reg(imx766, IMX766_REG_MODE_SELECT,
			       1, IMX766_MODE_STREAMING);
	if (ret) {
		dev_err(imx766->dev, "fail to start streaming\n");
		return ret;
	}

	return 0;
}

/**
 * imx766_stop_streaming() - Stop sensor stream
 * @imx766: pointer to imx766 device
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_stop_streaming(struct imx766 *imx766)
{
	return imx766_write_reg(imx766, IMX766_REG_MODE_SELECT,
				1, IMX766_MODE_STANDBY);
}

/**
 * imx766_set_stream() - Enable sensor streaming
 * @sd: pointer to imx766 subdevice
 * @enable: set to enable sensor streaming
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_set_stream(struct v4l2_subdev *sd, int enable)
{
	struct imx766 *imx766 = to_imx766(sd);
	int ret;

	mutex_lock(&imx766->mutex);

	if (enable) {
		ret = pm_runtime_resume_and_get(imx766->dev);
		if (ret)
			goto error_unlock;

		ret = imx766_start_streaming(imx766);
		if (ret)
			goto error_power_off;
	} else {
		imx766_stop_streaming(imx766);
		pm_runtime_put(imx766->dev);
	}

	mutex_unlock(&imx766->mutex);

	return 0;

error_power_off:
	pm_runtime_put(imx766->dev);
error_unlock:
	mutex_unlock(&imx766->mutex);

	return ret;
}

/**
 * imx766_detect() - Detect imx766 sensor
 * @imx766: pointer to imx766 device
 *
 * Return: 0 if successful, -EIO if sensor id does not match
 */
static int imx766_detect(struct imx766 *imx766)
{
	int ret;
	u32 val;

	ret = imx766_read_reg(imx766, IMX766_REG_ID, 2, &val);
	if (ret)
		return ret;

	dev_info(imx766->dev, "chip id 0x%x\n", val);

	if (val != IMX766_ID) {
		dev_err(imx766->dev, "chip id mismatch: %x!=%x\n",
			IMX766_ID, val);
		return -ENXIO;
	}

	return 0;
}

/**
 * imx766_parse_hw_config() - Parse HW configuration and check if supported
 * @imx766: pointer to imx766 device
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_parse_hw_config(struct imx766 *imx766)
{
	struct fwnode_handle *fwnode = dev_fwnode(imx766->dev);
	struct v4l2_fwnode_endpoint bus_cfg = {};
	struct fwnode_handle *ep;
	unsigned long rate;
	unsigned int i;
	int ret;

	if (!fwnode)
		return -ENXIO;

	/* Request optional reset pin */
	imx766->reset_gpio = devm_gpiod_get_optional(imx766->dev, "reset",
						     GPIOD_OUT_HIGH);
	if (IS_ERR(imx766->reset_gpio)) {
		dev_err(imx766->dev, "failed to get reset gpio %ld\n",
			PTR_ERR(imx766->reset_gpio));
		return PTR_ERR(imx766->reset_gpio);
	}

	/* Get sensor input clock */
	imx766->inclk = devm_clk_get(imx766->dev, NULL);
	if (IS_ERR(imx766->inclk)) {
		dev_err(imx766->dev, "could not get inclk\n");
		return PTR_ERR(imx766->inclk);
	}

	rate = clk_get_rate(imx766->inclk);
	if (rate != IMX766_INCLK_RATE) {
		dev_err(imx766->dev, "inclk frequency mismatch: %lu\n", rate);
		return -EINVAL;
	}

	/* Get optional DT defined regulators */
	for (i = 0; i < ARRAY_SIZE(imx766_supply_names); i++)
		imx766->supplies[i].supply = imx766_supply_names[i];

	ret = devm_regulator_bulk_get(imx766->dev,
				      ARRAY_SIZE(imx766_supply_names),
				      imx766->supplies);
	if (ret)
		return ret;

	ep = fwnode_graph_get_next_endpoint(fwnode, NULL);
	if (!ep)
		return -ENXIO;

	ret = v4l2_fwnode_endpoint_alloc_parse(ep, &bus_cfg);
	fwnode_handle_put(ep);
	if (ret)
		return ret;

	if (bus_cfg.bus_type != V4L2_MBUS_CSI2_CPHY) {
		dev_err(imx766->dev, "selected bus-type is not supported\n");
		ret = -EINVAL;
		goto done_endpoint_free;
	}

	if (bus_cfg.bus.mipi_csi2.num_data_lanes != IMX766_NUM_DATA_LANES) {
		dev_err(imx766->dev,
			"number of CSI2 data lanes %d is not supported\n",
			bus_cfg.bus.mipi_csi2.num_data_lanes);
		ret = -EINVAL;
		goto done_endpoint_free;
	}

	if (!bus_cfg.nr_of_link_frequencies) {
		dev_err(imx766->dev, "no link frequencies defined\n");
		ret = -EINVAL;
		goto done_endpoint_free;
	}

	for (i = 0; i < bus_cfg.nr_of_link_frequencies; i++)
		if (bus_cfg.link_frequencies[i] == IMX766_LINK_FREQ)
			goto done_endpoint_free;

	ret = -EINVAL;

done_endpoint_free:
	v4l2_fwnode_endpoint_free(&bus_cfg);

	return ret;
}

/* V4l2 subdevice ops */
static const struct v4l2_subdev_video_ops imx766_video_ops = {
	.s_stream = imx766_set_stream,
};

static const struct v4l2_subdev_pad_ops imx766_pad_ops = {
	.enum_mbus_code = imx766_enum_mbus_code,
	.enum_frame_size = imx766_enum_frame_size,
	.get_fmt = imx766_get_pad_format,
	.set_fmt = imx766_set_pad_format,
};

static const struct v4l2_subdev_ops imx766_subdev_ops = {
	.video = &imx766_video_ops,
	.pad = &imx766_pad_ops,
};

static const struct v4l2_subdev_internal_ops imx766_internal_ops = {
	.init_state = imx766_init_state,
};

/**
 * imx766_power_on() - Sensor power on sequence
 * @dev: pointer to i2c device
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_power_on(struct device *dev)
{
	struct v4l2_subdev *sd = dev_get_drvdata(dev);
	struct imx766 *imx766 = to_imx766(sd);
	int i, ret;

	for (i = 0; i < ARRAY_SIZE(imx766_supply_names); i++) {
		ret = regulator_enable(imx766->supplies[i].consumer);
		if (ret) {
			dev_err(dev, "failed to enable %s\n",
				imx766_supply_names[i]);
			goto error_regulators;
		}
	}

	usleep_range(1000, 1200);

	gpiod_set_value_cansleep(imx766->reset_gpio, 0);

	ret = clk_prepare_enable(imx766->inclk);
	if (ret) {
		dev_err(imx766->dev, "fail to enable inclk\n");
		goto error_reset;
	}

	usleep_range(2000, 2200);

	return 0;

error_reset:
	gpiod_set_value_cansleep(imx766->reset_gpio, 1);
error_regulators:
	while (--i >= 0)
		regulator_disable(imx766->supplies[i].consumer);

	return ret;
}

/**
 * imx766_power_off() - Sensor power off sequence
 * @dev: pointer to i2c device
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_power_off(struct device *dev)
{
	struct v4l2_subdev *sd = dev_get_drvdata(dev);
	struct imx766 *imx766 = to_imx766(sd);

	clk_disable_unprepare(imx766->inclk);

	gpiod_set_value_cansleep(imx766->reset_gpio, 1);

	for (int i = ARRAY_SIZE(imx766_supply_names) - 1; i >= 0; i--)
		regulator_disable(imx766->supplies[i].consumer);

	return 0;
}

/**
 * imx766_init_controls() - Initialize sensor subdevice controls
 * @imx766: pointer to imx766 device
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_init_controls(struct imx766 *imx766)
{
	struct v4l2_fwnode_device_properties props;
	struct v4l2_ctrl_handler *ctrl_hdlr = &imx766->ctrl_handler;
	const struct imx766_mode *mode = imx766->cur_mode;
	u32 lpfr;
	int ret;

	/* set properties from fwnode (e.g. rotation, orientation) */
	ret = v4l2_fwnode_device_parse(imx766->dev, &props);
	if (ret)
		return ret;

	ret = v4l2_ctrl_handler_init(ctrl_hdlr, 8);
	if (ret)
		return ret;

	/* Serialize controls with sensor device */
	ctrl_hdlr->lock = &imx766->mutex;

	/* Initialize exposure and gain */
	lpfr = mode->vblank + mode->height;
	imx766->exp_ctrl = v4l2_ctrl_new_std(ctrl_hdlr,
					     &imx766_ctrl_ops,
					     V4L2_CID_EXPOSURE,
					     IMX766_EXPOSURE_MIN,
					     lpfr - IMX766_EXPOSURE_OFFSET,
					     IMX766_EXPOSURE_STEP,
					     IMX766_EXPOSURE_DEFAULT);

	imx766->again_ctrl = v4l2_ctrl_new_std(ctrl_hdlr,
					       &imx766_ctrl_ops,
					       V4L2_CID_ANALOGUE_GAIN,
					       IMX766_AGAIN_MIN,
					       IMX766_AGAIN_MAX,
					       IMX766_AGAIN_STEP,
					       IMX766_AGAIN_DEFAULT);

	v4l2_ctrl_cluster(2, &imx766->exp_ctrl);

	imx766->vblank_ctrl = v4l2_ctrl_new_std(ctrl_hdlr,
						&imx766_ctrl_ops,
						V4L2_CID_VBLANK,
						mode->vblank_min,
						mode->vblank_max,
						1, mode->vblank);

	/* Read only controls */
	imx766->pclk_ctrl = v4l2_ctrl_new_std(ctrl_hdlr,
					      &imx766_ctrl_ops,
					      V4L2_CID_PIXEL_RATE,
					      mode->pclk, mode->pclk,
					      1, mode->pclk);

	imx766->link_freq_ctrl = v4l2_ctrl_new_int_menu(ctrl_hdlr,
							&imx766_ctrl_ops,
							V4L2_CID_LINK_FREQ,
							ARRAY_SIZE(link_freq) -
							1,
							mode->link_freq_idx,
							link_freq);
	if (imx766->link_freq_ctrl)
		imx766->link_freq_ctrl->flags |= V4L2_CTRL_FLAG_READ_ONLY;

	imx766->hblank_ctrl = v4l2_ctrl_new_std(ctrl_hdlr,
						&imx766_ctrl_ops,
						V4L2_CID_HBLANK,
						IMX766_REG_MIN,
						IMX766_REG_MAX,
						1, mode->hblank);
	if (imx766->hblank_ctrl)
		imx766->hblank_ctrl->flags |= V4L2_CTRL_FLAG_READ_ONLY;

	v4l2_ctrl_new_fwnode_properties(ctrl_hdlr, &imx766_ctrl_ops, &props);

	if (ctrl_hdlr->error) {
		dev_err(imx766->dev, "control init failed: %d\n",
			ctrl_hdlr->error);
		v4l2_ctrl_handler_free(ctrl_hdlr);
		return ctrl_hdlr->error;
	}

	imx766->sd.ctrl_handler = ctrl_hdlr;

	return 0;
}

/**
 * imx766_probe() - I2C client device binding
 * @client: pointer to i2c client device
 *
 * Return: 0 if successful, error code otherwise.
 */
static int imx766_probe(struct i2c_client *client)
{
	struct imx766 *imx766;
	int ret;

	imx766 = devm_kzalloc(&client->dev, sizeof(*imx766), GFP_KERNEL);
	if (!imx766)
		return -ENOMEM;

	imx766->dev = &client->dev;

	/* Initialize subdev */
	v4l2_i2c_subdev_init(&imx766->sd, client, &imx766_subdev_ops);
	imx766->sd.internal_ops = &imx766_internal_ops;

	ret = imx766_parse_hw_config(imx766);
	if (ret) {
		dev_err(imx766->dev, "HW configuration is not supported\n");
		return ret;
	}

	mutex_init(&imx766->mutex);

	ret = imx766_power_on(imx766->dev);
	if (ret) {
		dev_err(imx766->dev, "failed to power-on the sensor\n");
		goto error_mutex_destroy;
	}

	/* Check module identity */
	ret = imx766_detect(imx766);
	if (ret) {
		dev_err(imx766->dev, "failed to find sensor: %d\n", ret);
		goto error_power_off;
	}

	/* Default to the light binned mode */
	imx766->cur_mode = &supported_modes[0];
	imx766->vblank = imx766->cur_mode->vblank;

	ret = imx766_init_controls(imx766);
	if (ret) {
		dev_err(imx766->dev, "failed to init controls: %d\n", ret);
		goto error_power_off;
	}

	/* Initialize subdev */
	imx766->sd.flags |= V4L2_SUBDEV_FL_HAS_DEVNODE;
	imx766->sd.entity.function = MEDIA_ENT_F_CAM_SENSOR;

	/* Initialize source pad */
	imx766->pad.flags = MEDIA_PAD_FL_SOURCE;
	ret = media_entity_pads_init(&imx766->sd.entity, 1, &imx766->pad);
	if (ret) {
		dev_err(imx766->dev, "failed to init entity pads: %d\n", ret);
		goto error_handler_free;
	}

	ret = v4l2_async_register_subdev_sensor(&imx766->sd);
	if (ret < 0) {
		dev_err(imx766->dev,
			"failed to register async subdev: %d\n", ret);
		goto error_media_entity;
	}

	pm_runtime_set_active(imx766->dev);
	pm_runtime_enable(imx766->dev);
	pm_runtime_idle(imx766->dev);

	return 0;

error_media_entity:
	media_entity_cleanup(&imx766->sd.entity);
error_handler_free:
	v4l2_ctrl_handler_free(imx766->sd.ctrl_handler);
error_power_off:
	imx766_power_off(imx766->dev);
error_mutex_destroy:
	mutex_destroy(&imx766->mutex);

	return ret;
}

static void imx766_remove(struct i2c_client *client)
{
	struct v4l2_subdev *sd = i2c_get_clientdata(client);
	struct imx766 *imx766 = to_imx766(sd);

	v4l2_async_unregister_subdev(sd);
	media_entity_cleanup(&sd->entity);
	v4l2_ctrl_handler_free(sd->ctrl_handler);

	pm_runtime_disable(&client->dev);
	if (!pm_runtime_status_suspended(&client->dev))
		imx766_power_off(&client->dev);
	pm_runtime_set_suspended(&client->dev);

	mutex_destroy(&imx766->mutex);
}

static const struct dev_pm_ops imx766_pm_ops = {
	SET_RUNTIME_PM_OPS(imx766_power_off, imx766_power_on, NULL)
};

static const struct of_device_id imx766_of_match[] = {
	{ .compatible = "sony,imx766" },
	{ /* sentinel */ }
};
MODULE_DEVICE_TABLE(of, imx766_of_match);

static struct i2c_driver imx766_driver = {
	.probe = imx766_probe,
	.remove = imx766_remove,
	.driver = {
		.name = "imx766",
		.pm = &imx766_pm_ops,
		.of_match_table = imx766_of_match,
	},
};

module_i2c_driver(imx766_driver);

MODULE_DESCRIPTION("Sony IMX766 sensor driver");
MODULE_LICENSE("GPL");
