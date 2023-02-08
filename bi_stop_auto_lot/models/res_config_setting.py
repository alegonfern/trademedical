# -*- coding: utf-8 -*-
# Part of Browseinfo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    auto_lot_stop = fields.Boolean('Stop auto lot number')


    @api.model
    def get_values(self):
        res = super(ResConfigSettings, self).get_values()
        res.update(auto_lot_stop = self.env['ir.config_parameter'].sudo().get_param('bi_stop_auto_lot.auto_lot_stop'))
        return res


    def set_values(self):
        super(ResConfigSettings, self).set_values()
        self.env['ir.config_parameter'].sudo().set_param('bi_stop_auto_lot.auto_lot_stop', self.auto_lot_stop)
        
        
