# -*- coding: utf-8 -*-
# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.


from datetime import datetime
from dateutil.relativedelta import relativedelta
from odoo.tools.misc import split_every
from odoo import api, fields, models, registry, _
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare, float_is_zero, float_repr, float_round
from odoo.osv import expression

import logging

_logger = logging.getLogger(__name__)


class Stockpicking_inherit(models.Model):
    _inherit = "stock.picking"

    def action_assign(self):
        res = super(Stockpicking_inherit, self).action_assign()
        if self._context.get('from_sale') or not self.env['ir.config_parameter'].sudo().get_param(
                'bi_stop_auto_lot.auto_lot_stop'):
            res = super(Stockpicking_inherit, self).action_assign()
        else:
            # this will remove move_line_id
            if self.move_ids_without_package.sudo():
                for mv_line_wp in self.move_ids_without_package:
                    for mv_line in mv_line_wp:
                        if mv_line.move_line_ids:
                            mv_line.move_line_ids.lot_id = False
            for picking in self:
                if picking.sale_id.warehouse_id.delivery_steps == 'ship_only':
                    picking.state = "assigned"
                else:
                    if picking.sale_id.warehouse_id.lot_stock_id == picking.location_id:
                        picking.state = "assigned"
        return res


class ProcurementGroup(models.Model):
    _inherit = 'procurement.group'

    @api.model
    def _run_scheduler_tasks(self, use_new_cursor=False, company_id=False):
        # Minimum stock rules
        domain = self._get_orderpoint_domain(company_id=company_id)
        orderpoints = self.env['stock.warehouse.orderpoint'].search(domain)
        # ensure that qty_* which depends on datetime.now() are correctly
        # recomputed
        orderpoints.sudo()._compute_qty_to_order()
        orderpoints.sudo()._procure_orderpoint_confirm(use_new_cursor=use_new_cursor, company_id=company_id,
                                                       raise_user_error=False)
        if use_new_cursor:
            self._cr.commit()

        # Search all confirmed stock_moves and try to assign them
        domain = self._get_moves_to_assign_domain(company_id)
        moves_to_assign = self.env['stock.move'].search(domain, limit=None,
                                                        order='priority desc, date asc')
        for moves_chunk in split_every(100, moves_to_assign.ids):
            if use_new_cursor:
                self._cr.commit()

        if use_new_cursor:
            self._cr.commit()

        # Merge duplicated quants
        self.env['stock.quant']._quant_tasks()


class Stockmove_inherit(models.Model):
    _inherit = "stock.move"

    def _update_reserved_quantity(self, need, available_quantity, location_id, lot_id=None, package_id=None,
                                  owner_id=None, strict=True):
        """ Create or update move lines.
        """
        if self._context.get('from_sale') == True:

            self.ensure_one()

            lots = []
            for line in self.move_line_ids:
                lots.append(line.lot_id.id)
            if lots:
                lot_id = self.env['stock.production.lot'].browse(lots)
            else:
                lot_id = self.env['stock.production.lot']
            if not package_id:
                package_id = self.env['stock.quant.package']
            if not owner_id:
                owner_id = self.env['res.partner']

            taken_quantity = min(available_quantity, need)

            # `taken_quantity` is in the quants unit of measure. There's a possibility that the move's
            # unit of measure won't be respected if we blindly reserve this quantity, a common usecase
            # is if the move's unit of measure's rounding does not allow fractional reservation. We chose
            # to convert `taken_quantity` to the move's unit of measure with a down rounding method and
            # then get it back in the quants unit of measure with an half-up rounding_method. This
            # way, we'll never reserve more than allowed. We do not apply this logic if
            # `available_quantity` is brought by a chained move line. In this case, `_prepare_move_line_vals`
            # will take care of changing the UOM to the UOM of the product.
            if not strict and self.product_id.uom_id != self.product_uom:
                taken_quantity_move_uom = self.product_id.uom_id._compute_quantity(taken_quantity, self.product_uom,
                                                                                   rounding_method='DOWN')
                taken_quantity = self.product_uom._compute_quantity(taken_quantity_move_uom, self.product_id.uom_id,
                                                                    rounding_method='HALF-UP')

            quants = []
            rounding = self.env['decimal.precision'].precision_get('Product Unit of Measure')

            if self.product_id.tracking == 'serial':
                if float_compare(taken_quantity, int(taken_quantity), precision_digits=rounding) != 0:
                    taken_quantity = 0

            try:
                with self.env.cr.savepoint():
                    if not float_is_zero(taken_quantity, precision_rounding=self.product_id.uom_id.rounding):
                        quants = self.env['stock.quant']._update_reserved_quantity(
                            self.product_id, location_id, taken_quantity, lot_id=lot_id,
                            package_id=package_id, owner_id=owner_id, strict=strict
                        )
            except UserError:
                taken_quantity = 0

            # Find a candidate move line to update or create a new one.
            for reserved_quant, quantity in quants:
                to_update = self.move_line_ids.filtered(
                    lambda ml: ml._reservation_is_updatable(quantity, reserved_quant))
                if to_update:
                    uom_quantity = self.product_id.uom_id._compute_quantity(quantity, to_update[0].product_uom_id,
                                                                            rounding_method='HALF-UP')
                    uom_quantity = float_round(uom_quantity, precision_digits=rounding)
                    uom_quantity_back_to_product_uom = to_update[0].product_uom_id._compute_quantity(uom_quantity,
                                                                                                     self.product_id.uom_id,
                                                                                                     rounding_method='HALF-UP')
                if to_update and float_compare(quantity, uom_quantity_back_to_product_uom,
                                               precision_digits=rounding) == 0:
                    to_update[0].with_context(bypass_reservation_update=True).product_uom_qty += uom_quantity
                else:
                    if self.product_id.tracking == 'serial':
                        for i in range(0, int(quantity)):
                            self.env['stock.move.line'].create(
                                self._prepare_move_line_vals(quantity=1, reserved_quant=reserved_quant))
                    else:
                        self.env['stock.move.line'].create(
                            self._prepare_move_line_vals(quantity=quantity, reserved_quant=reserved_quant))
            return taken_quantity

        else:
            return super(Stockmove_inherit, self)._update_reserved_quantity(need, available_quantity, location_id,
                                                                            lot_id=lot_id, package_id=package_id,
                                                                            owner_id=owner_id, strict=strict)


class Stockquant_inherit(models.Model):
    _inherit = "stock.quant"

    def _gather(self, product_id, location_id, lot_id=None, package_id=None, owner_id=None, strict=False):
        if self._context.get('from_sale') == True:
            self.env['stock.quant'].flush(['location_id', 'owner_id', 'package_id', 'lot_id', 'product_id'])
            self.env['product.product'].flush(['virtual_available'])
            removal_strategy = self._get_removal_strategy(product_id, location_id)
            removal_strategy_order = self._get_removal_strategy_order(removal_strategy)
            domain = [
                ('product_id', '=', product_id.id),
            ]

            if not strict:
                if lot_id and len(lot_id) == 1:
                    domain = expression.AND([['|', ('lot_id', '=', lot_id.id), ('lot_id', '=', False)], domain])
                if lot_id and len(lot_id) > 1:
                    domain = expression.AND([['|', ('lot_id', 'in', lot_id.ids), ('lot_id', '=', False)], domain])
                if package_id:
                    domain = expression.AND([[('package_id', '=', package_id.id)], domain])
                if owner_id:
                    domain = expression.AND([[('owner_id', '=', owner_id.id)], domain])
                domain = expression.AND([[('location_id', 'child_of', location_id.id)], domain])
            else:
                if lot_id and len(lot_id) == 1:
                    domain = expression.AND([['|', ('lot_id', '=', lot_id.id), ('lot_id', '=', False)] if lot_id else [
                        ('lot_id', '=', False)], domain])
                if lot_id and len(lot_id) > 1:
                    domain = expression.AND([['|', ('lot_id', 'in', lot_id.ids),
                                              ('lot_id', '=', False)] if lot_id else [('lot_id', '=', False)], domain])
                domain = expression.AND([[('package_id', '=', package_id and package_id.id or False)], domain])
                domain = expression.AND([[('owner_id', '=', owner_id and owner_id.id or False)], domain])
                domain = expression.AND([[('location_id', '=', location_id.id)], domain])

            # Copy code of _search for special NULLS FIRST/LAST order
            self.check_access_rights('read')
            query = self._where_calc(domain)
            self._apply_ir_rules(query, 'read')
            from_clause, where_clause, where_clause_params = query.get_sql()
            where_str = where_clause and (" WHERE %s" % where_clause) or ''
            query_str = 'SELECT "%s".id FROM ' % self._table + from_clause + where_str + " ORDER BY " + removal_strategy_order
            self._cr.execute(query_str, where_clause_params)
            res = self._cr.fetchall()
            # No uniquify list necessary as auto_join is not applied anyways...
            quants = self.browse([x[0] for x in res])
            quants = quants.sorted(lambda q: not q.lot_id)
            return quants
        else:
            return super(Stockquant_inherit, self)._gather(product_id, location_id, lot_id=lot_id,
                                                           package_id=package_id, owner_id=owner_id, strict=strict)
# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:
